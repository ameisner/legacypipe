'''
This script performs forced photometry of individual Legacy Survey
images given a data release catalog.
'''

from __future__ import print_function
import os
import sys
import shutil

import numpy as np
import fitsio

from astrometry.util.fits import fits_table, merge_tables
from astrometry.util.file import trymakedirs
from astrometry.util.ttime import Time

from tractor import Tractor, Catalog
from tractor.galaxy import disable_galaxy_cache

from legacypipe.survey import LegacySurveyData, bricks_touching_wcs, get_version_header, apertures_arcsec, radec_at_mjd
from legacypipe.catalog import read_fits_catalog
from legacypipe.outliers import read_outlier_mask_file

def get_parser():
    '''
    Returns the option parser for forced photometry of Legacy Survey images
    '''
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--threads', type=int, help='Run multi-threaded', default=None)

    parser.add_argument('--survey-dir', help='Override LEGACY_SURVEY_DIR')
    parser.add_argument('--cache-dir', help='Set cache dir to go with --survey-dir')
    parser.add_argument('--pre-cache', default=False, action='store_true',
                        help='Pre-copy image and calib files into the cache; requires --cache-dir')

    parser.add_argument('--catalog', help='Use the given FITS catalog file, rather than reading from a data release directory')

    parser.add_argument('--catalog-dir', help='Set LEGACY_SURVEY_DIR to use to read catalogs')

    parser.add_argument('--catalog-dir-north', help='Set LEGACY_SURVEY_DIR to use to read Northern catalogs')
    parser.add_argument('--catalog-dir-south', help='Set LEGACY_SURVEY_DIR to use to read Southern catalogs')
    parser.add_argument('--catalog-resolve-dec-ngc', type=float, help='Dec at which to switch from Northern to Southern catalogs (NGC only)')

    parser.add_argument('--skip-calibs', dest='do_calib', default=True, action='store_false',
                        help='Do not try to run calibrations')
    parser.add_argument('--skip', dest='skip', default=False, action='store_true',
                        help='Exit if the output file already exists')

    parser.add_argument('--zoom', type=int, nargs=4, help='Set target image extent (eg "0 100 0 200")')
    parser.add_argument('--no-ceres', action='store_false', dest='ceres', help='Do not use Ceres optimization engine (use scipy)')

    parser.add_argument('--ceres-threads', type=int, default=1,
                        help='Set number of threads used by Ceres')

    parser.add_argument('--plots', default=None, help='Create plots; specify a base filename for the plots')
    parser.add_argument('--write-cat', help='Write out the catalog subset on which forced phot was done')
    parser.add_argument('--apphot', action='store_true',
                      help='Do aperture photometry?')
    parser.add_argument('--no-forced', dest='forced', action='store_false',
                      help='Do NOT do regular forced photometry?  Implies --apphot')

    parser.add_argument('--derivs', action='store_true',
                        help='Include RA,Dec derivatives in forced photometry?')

    parser.add_argument('--agn', action='store_true',
                        help='Add a point source to the center of each DEV/EXP/SER galaxy?')

    parser.add_argument('--constant-invvar', action='store_true',
                        help='Set inverse-variance to a constant across the image?')

    parser.add_argument('--no-hybrid-psf', dest='hybrid_psf', action='store_false',
                        default=True,
                        help='Do not use hybrid pixelized-MoG PSF model?')
    parser.add_argument('--no-normalize-psf', dest='normalize_psf', action='store_false',
                        default=True,
                        help='Do not normalize PSF?')

    parser.add_argument('--no-move-gaia', dest='move_gaia', action='store_false',
                        default=True, help='Do not move Gaia stars to image epoch?')

    parser.add_argument('--save-model',
                        help='Compute and save model image?')
    parser.add_argument('--save-data',
                        help='Compute and save model image?')

    parser.add_argument('--camera', help='Cut to only CCD with given camera name?')

    parser.add_argument('--expnum', type=int, help='Exposure number')
    parser.add_argument('--ccdname', default=None, help='CCD name to cut to (default: all)')
    parser.add_argument('--out', help='Output catalog filename (default: use --out-dir)')
    parser.add_argument('--out-dir', help='Output base directory')

    parser.add_argument('--outlier-mask', nargs='?', const='default',
                        help='Write the reassembled outlier mask?  Optionally include output filename; default use --out-dir')
    parser.add_argument('-v', '--verbose', dest='verbose', action='count',
                        default=0, help='Make more verbose')
    return parser

def main(survey=None, opt=None, args=None):
    '''Driver function for forced photometry of individual Legacy
    Survey images.
    '''
    if args is None:
        args = sys.argv[1:]
    print('forced_photom.py', ' '.join(args))

    if opt is None:
        parser = get_parser()
        opt = parser.parse_args(args)

    import logging
    if opt.verbose == 0:
        lvl = logging.INFO
    else:
        lvl = logging.DEBUG
    logging.basicConfig(level=lvl, format='%(message)s', stream=sys.stdout)
    # tractor logging is *soooo* chatty
    logging.getLogger('tractor.engine').setLevel(lvl + 10)

    t0 = Time()
    if survey is None:
        survey = LegacySurveyData(survey_dir=opt.survey_dir,
                                  cache_dir=opt.cache_dir,
                                  output_dir=opt.out_dir)
    if opt.skip:
        if opt.out is not None:
            outfn = opt.out
        else:
            outfn = survey.find_file('forced', output=True,
                                     camera=opt.camera, expnum=opt.expnum)
        if os.path.exists(outfn):
            print('Ouput file exists:', outfn)
            return 0

    if opt.derivs and opt.agn:
        print('Sorry, can\'t do --derivs AND --agn')
        return -1

    if opt.out is None and opt.out_dir is None:
        print('Must supply either --out or --out-dir')
        return -1

    if opt.expnum is None and opt.out is None:
        print('If no --expnum is given, must supply --out filename')
        return -1

    if not opt.forced:
        opt.apphot = True

    zoomslice = None
    if opt.zoom is not None:
        (x0,x1,y0,y1) = opt.zoom
        zoomslice = (slice(y0,y1), slice(x0,x1))

    ps = None
    if opt.plots is not None:
        from astrometry.util.plotutils import PlotSequence
        ps = PlotSequence(opt.plots)

    # Cache CCDs files before the find_ccds call...
    # Copy required files into the cache?
    if opt.pre_cache:
        def copy_files_to_cache(fns):
            for fn in fns:
                cachefn = fn.replace(survey.survey_dir, survey.cache_dir)
                if not cachefn.startswith(survey.cache_dir):
                    print('Skipping', fn)
                    continue
                outdir = os.path.dirname(cachefn)
                trymakedirs(outdir)
                print('Copy', fn)
                print('  to', cachefn)
                shutil.copyfile(fn, cachefn)
        assert(survey.cache_dir is not None)
        fnset = set()
        fn = survey.find_file('bricks')
        fnset.add(fn)
        fns = survey.find_file('ccd-kds')
        fnset.update(fns)
        copy_files_to_cache(fnset)

    # Read metadata from survey-ccds.fits table
    ccds = survey.find_ccds(camera=opt.camera, expnum=opt.expnum,
                         ccdname=opt.ccdname)
    print(len(ccds), 'with camera', opt.camera, 'and expnum', opt.expnum, 'and ccdname', opt.ccdname)
    # sort CCDs
    ccds.cut(np.lexsort((ccds.ccdname, ccds.expnum, ccds.camera)))

    # If there is only one catalog survey_dir, we pass it to get_catalog_in_wcs
    # as the northern survey.
    catsurvey_north = survey
    catsurvey_south = None

    if opt.catalog_dir_north is not None:
        assert(opt.catalog_dir_south is not None)
        assert(opt.catalog_resolve_dec_ngc is not None)
        catsurvey_north = LegacySurveyData(survey_dir = opt.catalog_dir_north)
        catsurvey_south = LegacySurveyData(survey_dir = opt.catalog_dir_south)
    elif opt.catalog_dir is not None:
        catsurvey_north = LegacySurveyData(survey_dir = opt.catalog_dir)

    # Copy required CCD & calib files into the cache?
    if opt.pre_cache:
        assert(survey.cache_dir is not None)
        fnset = set()
        for ccd in ccds:
            im = survey.get_image_object(ccd)
            for key in im.get_cacheable_filename_variables():
                fn = getattr(im, key)
                if fn is None or not(os.path.exists(fn)):
                    continue
                fnset.add(fn)
        copy_files_to_cache(fnset)

    args = []
    for ccd in ccds:
        args.append((survey,
                     catsurvey_north, catsurvey_south, opt.catalog_resolve_dec_ngc,
                     ccd, opt, zoomslice, None, ps))

    if opt.threads:
        from astrometry.util.multiproc import multiproc
        from astrometry.util.timingpool import TimingPool, TimingPoolMeas
        pool = TimingPool(opt.threads)
        poolmeas = TimingPoolMeas(pool, pickleTraffic=False)
        Time.add_measurement(poolmeas)
        mp = multiproc(None, pool=pool)
        tm = Time()
        FF = mp.map(bounce_one_ccd, args)
        print('Multi-processing forced-phot:', Time()-tm)
        del mp
        Time.measurements.remove(poolmeas)
        del poolmeas
        pool.close()
        pool.join()
        del pool
    else:
        FF = map(bounce_one_ccd, args)

    FF = [F for F in FF if F is not None]
    if len(FF) == 0:
        print('No photometry results to write.')
        return 0
    # Keep only the first header
    _,version_hdr,_,_ = FF[0]
    # unpack results
    outlier_masks = [m for _,_,m,_ in FF]
    outlier_hdrs  = [h for _,_,_,h in FF]
    FF            = [F for F,_,_,_ in FF]
    F = merge_tables(FF)

    if len(ccds):
        version_hdr.delete('CPHDU')
        version_hdr.delete('CCDNAME')

    from legacypipe.utils import add_bits
    from legacypipe.bits import DQ_BITS
    add_bits(version_hdr, DQ_BITS, 'DQMASK', 'DQ', 'D')
    from legacyzpts.psfzpt_cuts import CCD_CUT_BITS
    add_bits(version_hdr, CCD_CUT_BITS, 'CCD_CUTS', 'CC', 'C')
    for i,ap in enumerate(apertures_arcsec):
        version_hdr.add_record(dict(name='APRAD%i' % i, value=ap,
                                    comment='(optical) Aperture radius, in arcsec'))

    from legacypipe.units import get_units_for_columns

    columns = F.get_columns()
    order = ['release', 'brickid', 'brickname', 'objid', 'camera', 'expnum',
             'ccdname', 'filter', 'mjd', 'exptime', 'psfsize', 'fwhm', 'ccd_cuts',
             'airmass', 'sky', 'skyrms', 'psfdepth', 'galdepth', 'ccdzpt',
             'ccdrarms', 'ccddecrms', 'ccdphrms', 'ra', 'dec', 'flux',
             'flux_ivar', 'fracflux', 'rchisq', 'fracmasked', 'fracin',
             'apflux', 'apflux_ivar', 'x', 'y', 'dqmask', 'dra', 'ddec',
             'dra_ivar', 'ddec_ivar']
    columns = [c for c in order if c in columns]
    units = get_units_for_columns(columns)

    if opt.out is not None:
        outdir = os.path.dirname(opt.out)
        if len(outdir):
            trymakedirs(outdir)
        tmpfn = os.path.join(outdir, 'tmp-' + os.path.basename(opt.out))
        fitsio.write(tmpfn, None, header=version_hdr, clobber=True)
        F.writeto(tmpfn, units=units, append=True, columns=columns)
        os.rename(tmpfn, opt.out)
        print('Wrote', opt.out)
    else:
        with survey.write_output('forced', camera=opt.camera, expnum=opt.expnum) as out:
            F.writeto(None, fits_object=out.fits, primheader=version_hdr,
                      units=units, columns=columns)
            print('Wrote', out.real_fn)

    if opt.outlier_mask is not None:
        # Add outlier bit meanings to the primary header
        version_hdr.add_record(dict(name='COMMENT', value='Outlier mask bit meanings'))
        version_hdr.add_record(dict(name='OUTL_POS', value=1,
                                    comment='Outlier mask bit for Positive outlier'))
        version_hdr.add_record(dict(name='OUTL_NEG', value=2,
                                    comment='Outlier mask bit for Negative outlier'))

    if opt.outlier_mask == 'default':
        outdir = os.path.join(opt.out_dir, 'outlier-masks')
        camexp = set(zip(ccds.camera, ccds.expnum))
        for c,e in camexp:
            I = np.flatnonzero((ccds.camera == c) * (ccds.expnum == e))
            ccd = ccds[I[0]]
            imfn = ccd.image_filename.strip()
            outfn = os.path.join(outdir, imfn.replace('.fits', '-outlier.fits'))
            trymakedirs(outfn, dir=True)
            tempfn = outfn.replace('.fits', '-tmp.fits')
            with fitsio.FITS(tempfn, 'rw', clobber=True) as fits:
                fits.write(None, header=version_hdr)
                for i in I:
                    mask = outlier_masks[i]
                    _,_,_,meth,tile = survey.get_compression_args('outliers_mask', shape=mask.shape)
                    fits.write(mask, header=outlier_hdrs[i], extname=ccds.ccdname[i],
                               compress=meth, tile_dims=tile)
            os.rename(tempfn, outfn)
            print('Wrote', outfn)
    elif opt.outlier_mask is not None:
        with fitsio.FITS(opt.outlier_mask, 'rw', clobber=True) as F:
            F.write(None, header=version_hdr)
            for i,(hdr,mask) in enumerate(zip(outlier_hdrs,outlier_masks)):
                _,_,_,meth,tile = survey.get_compression_args('outliers_mask', shape=mask.shape)
                F.write(mask, header=hdr, extname=ccds.ccdname[i],
                        compress=meth, tile_dims=tile)
        print('Wrote', opt.outlier_mask)

    tnow = Time()
    print('Total:', tnow-t0)
    return 0

def bounce_one_ccd(X):
    # for multiprocessing
    return forced_photom_one_ccd(*X)

def get_catalog_in_wcs(chipwcs, survey, catsurvey_north, catsurvey_south=None,
                       resolve_dec=None, margin=20):
    TT = []
    surveys = [(catsurvey_north, True)]
    if catsurvey_south is not None:
        surveys.append((catsurvey_south, False))

    columns = ['ra', 'dec', 'brick_primary', 'type', 'release',
               'brickid', 'brickname', 'objid',
               'sersic', 'shape_r', 'shape_e1', 'shape_e2',
               'ref_epoch', 'pmra', 'pmdec', 'parallax', 'ref_cat', 'ref_id',]

    for catsurvey,north in surveys:
        bricks = bricks_touching_wcs(chipwcs, survey=catsurvey)

        if resolve_dec is not None:
            from astrometry.util.starutil_numpy import radectolb
            bricks.gal_l, bricks.gal_b = radectolb(bricks.ra, bricks.dec)

        for b in bricks:
            # Skip bricks that are entirely on the wrong side of the resolve line (NGC only)
            if resolve_dec is not None:
                # Northern survey, brick too far south (max dec is below the resolve line)
                if north and b.dec2 <= resolve_dec:
                    continue
                # Southern survey, brick too far north (min dec is above the resolve line), but only in the North Galactic Cap
                if not(north) and b.dec1 >= resolve_dec and b.gal_b > 0:
                     continue
            # there is some overlap with this brick... read the catalog.
            fn = catsurvey.find_file('tractor', brick=b.brickname)
            if not os.path.exists(fn):
                print('WARNING: catalog', fn, 'does not exist.  Skipping!')
                continue
            print('Reading', fn)
            T = fits_table(fn, columns=columns)
            if resolve_dec is not None:
                if north:
                    T.cut(T.dec >= resolve_dec)
                    print('Cut to', len(T), 'north of the resolve line')
                elif b.gal_b > 0:
                    # Northern galactic cap only: cut Southern survey
                    T.cut(T.dec <  resolve_dec)
                    print('Cut to', len(T), 'south of the resolve line')
            _,xx,yy = chipwcs.radec2pixelxy(T.ra, T.dec)
            W,H = chipwcs.get_width(), chipwcs.get_height()
            # Cut to sources that are inside the image+margin
            T.cut((xx >= -margin) * (xx <= (W+margin)) *
                  (yy >= -margin) * (yy <= (H+margin)))
            T.cut(T.brick_primary)
            #print('Cut to', len(T), 'on brick_primary')
            # drop DUP sources
            I, = np.nonzero([t.strip() != 'DUP' for t in T.type])
            T.cut(I)
            #print('Cut to', len(T), 'after removing DUP')
            if len(T):
                TT.append(T)
    if len(TT) == 0:
        return None
    T = merge_tables(TT, columns='fillzero')
    T._header = TT[0]._header
    del TT

    SGA = find_missing_sga(T, chipwcs, survey, surveys, columns)
    if SGA is not None:
        ## Add 'em in!
        T = merge_tables([T, SGA], columns='fillzero')
    print('Total of', len(T), 'catalog sources')
    return T

def find_missing_sga(T, chipwcs, survey, surveys, columns):
    # Look up SGA large galaxies touching this chip.
    # The ones inside this chip(+margin) will already exist in the catalog;
    # we'll find the ones we're missing and read those extra brick catalogs.
    from legacypipe.reference import read_large_galaxies
    # Find all the SGA sources we need
    sga = read_large_galaxies(survey, chipwcs, bands=None, extra_columns=['brickname'])
    if sga is None:
        print('No SGA galaxies found')
        return None
    sga.cut(sga.islargegalaxy * sga.freezeparams)
    if len(sga) == 0:
        print('No frozen SGA galaxies found')
        return None
    # keep_radius to pix
    keeprad = np.ceil(sga.keep_radius * 3600. / chipwcs.pixel_scale()).astype(int)
    _,xx,yy = chipwcs.radec2pixelxy(sga.ra, sga.dec)
    H,W = chipwcs.shape
    # cut to those touching the chip
    sga.cut((xx > -keeprad) * (xx < W+keeprad) *
            (yy > -keeprad) * (yy < H+keeprad))
    #print('Read', len(sga), 'SGA galaxies touching the chip.')
    if len(sga) == 0:
        print('No SGA galaxies touch this chip')
        return None
    Tsga = T[T.ref_cat == 'L3']
    #print(len(Tsga), 'SGA entries already exist in catalog')
    Isga = np.array([i for i,sga_id in enumerate(sga.ref_id) if not sga_id in set(Tsga.ref_id)])
    assert(len(Isga) + len(Tsga) == len(sga))
    if len(Isga) == 0:
        print('All SGA galaxies already in catalogs')
        return None
    print('Finding', len(Isga), 'additional SGA entries in nearby bricks')
    sga.cut(Isga)
    #print('Finding bricks to read...')
    sgabricks = []

    for ra,dec,brick in zip(sga.ra, sga.dec, sga.brickname):
        bricks = survey.get_bricks_by_name(brick)
        brick = bricks[0]
        # The SGA catalog has a "brickname", but it unfortunately is not always exactly correct
        if ra >= brick.ra1 and ra < brick.ra2 and dec >= brick.dec1 and dec < brick.dec2:
            sgabricks.append(bricks)
        else:
            # MAGIC 0.2 ~ brick radius
            bricks = survey.get_bricks_near(ra, dec, 0.2)
            bricks = bricks[(ra  >= bricks.ra1 ) * (ra  < bricks.ra2) *
                            (dec >= bricks.dec1) * (dec < bricks.dec2)]
            if len(bricks):
                sgabricks.append(bricks)
    sgabricks = merge_tables(sgabricks)
    _,I = np.unique(sgabricks.brickname, return_index=True)
    sgabricks.cut(I)
    #print('Need to read', len(sgabricks), 'bricks to pick up SGA sources')
    SGA = []
    for brick in sgabricks.brickname:
        # For picking up these SGA bricks, resolve doesn't matter (they're fixed
        # in both).
        for catsurvey,_ in surveys:
            fn = catsurvey.find_file('tractor', brick=brick)
            if os.path.exists(fn):
                t = fits_table(fn, columns=['ref_cat', 'ref_id'])
                I = np.flatnonzero(t.ref_cat == 'L3')
                #print('Read', len(I), 'SGA entries from', brick)
                SGA.append(fits_table(fn, columns=columns, rows=I))
                break
    SGA = merge_tables(SGA)
    SGA.cut(SGA.brick_primary)
    #print('Total of', len(SGA), 'sources')
    I = np.array([i for i,ref_id in enumerate(SGA.ref_id) if ref_id in set(sga.ref_id)])
    SGA.cut(I)
    #print('Found', len(SGA), 'desired SGA sources')
    assert(len(sga) == len(SGA))
    assert(set(sga.ref_id) == set(SGA.ref_id))
    return SGA

def forced_photom_one_ccd(survey, catsurvey_north, catsurvey_south, resolve_dec,
                          ccd, opt, zoomslice, radecpoly, ps):
    from functools import reduce
    from legacypipe.bits import DQ_BITS

    tlast = Time()
    #print('Opt:', opt)
    im = survey.get_image_object(ccd)
    print('Forced_photom_one_ccd: checking cache', survey.cache_dir)
    if survey.cache_dir is not None:
        im.check_for_cached_files(survey)
    if opt.do_calib:
        im.run_calibs(splinesky=True)

    tim = im.get_tractor_image(slc=zoomslice,
                               radecpoly=radecpoly,
                               pixPsf=True,
                               hybridPsf=opt.hybrid_psf,
                               normalizePsf=opt.normalize_psf,
                               constant_invvar=opt.constant_invvar,
                               old_calibs_ok=True,
                               trim_edges=False)
    print('Got tim:', tim)
    if tim is None:
        return None
    chipwcs = tim.subwcs
    H,W = tim.shape

    tnow = Time()
    print('Read image:', tnow-tlast)
    tlast = tnow

    if ccd.camera == 'decam':
        # Halo subtraction
        from legacypipe.halos import subtract_one
        from legacypipe.reference import mask_radius_for_mag, read_gaia
        ref_margin = mask_radius_for_mag(0.)
        mpix = int(np.ceil(ref_margin * 3600. / chipwcs.pixel_scale()))
        marginwcs = chipwcs.get_subimage(-mpix, -mpix, W+2*mpix, H+2*mpix)
        gaia = read_gaia(marginwcs, None)
        keeprad = np.ceil(gaia.keep_radius * 3600. / chipwcs.pixel_scale()).astype(int)
        _,xx,yy = chipwcs.radec2pixelxy(gaia.ra, gaia.dec)
        # cut to those touching the chip
        gaia.cut((xx > -keeprad) * (xx < W+keeprad) *
                 (yy > -keeprad) * (yy < H+keeprad))
        Igaia, = np.nonzero(gaia.isgaia * gaia.pointsource)
        halostars = gaia[Igaia]
        print('Got', len(gaia), 'Gaia stars,', len(halostars), 'for halo subtraction')
        moffat = True
        halos = subtract_one((tim, halostars, moffat))
        tim.data -= halos

    # The "north" and "south" directories often don't have
    # 'survey-bricks" files of their own -- use the 'survey' one
    # instead.
    if catsurvey_south is not None:
        try:
            catsurvey_south.get_bricks_readonly()
        except:
            catsurvey_south.bricks = survey.get_bricks_readonly()
    if catsurvey_north is not None:
        try:
            catsurvey_north.get_bricks_readonly()
        except:
            catsurvey_north.bricks = survey.get_bricks_readonly()

    # Apply outlier masks
    outlier_header = None
    outlier_mask = None
    posneg_mask = None
    if opt.outlier_mask is not None:
        posneg_mask = np.zeros(tim.shape, np.uint8)

    # # Outliers masks are computed within a survey (eg north/south
    # # for dr9), and are stored in a brick-oriented way, in the
    # # results directories.
    bricks = bricks_touching_wcs(chipwcs, survey=survey)

    for b in bricks:
        print('Reading outlier mask for brick', b.brickname,
              ':', survey.find_file('outliers_mask', brick=b.brickname, output=False))
        ok = read_outlier_mask_file(survey, [tim], b.brickname, pos_neg_mask=posneg_mask,
                                    subimage=False, output=False, ps=ps)
        if not ok:
            print('WARNING: failed to read outliers mask file for brick', b.brickname)

    if opt.outlier_mask is not None:
        outlier_mask = np.zeros((ccd.height, ccd.width), np.uint8)
        outlier_mask[tim.y0:tim.y0+H, tim.x0:tim.x0+W] = posneg_mask
        del posneg_mask
        # Grab original image headers (including WCS)
        im = survey.get_image_object(ccd)
        imhdr = im.read_image_header()
        imhdr['CAMERA'] = ccd.camera
        imhdr['EXPNUM'] = ccd.expnum
        imhdr['CCDNAME'] = ccd.ccdname
        imhdr['IMGFILE'] = ccd.image_filename.strip()
        outlier_header = imhdr

    if opt.catalog:
        T = fits_table(opt.catalog)
    else:
        chipwcs = tim.subwcs
        T = get_catalog_in_wcs(chipwcs, survey, catsurvey_north, catsurvey_south=catsurvey_south,
                               resolve_dec=resolve_dec)
        if T is None:
            print('No sources to photometer.')
            return None
        if opt.write_cat:
            T.writeto(opt.write_cat)
            print('Wrote catalog to', opt.write_cat)

    surveydir = survey.get_survey_dir()
    del survey

    if opt.move_gaia:
        # Gaia stars: move RA,Dec to the epoch of this image.
        I = np.flatnonzero(T.ref_epoch > 0)
        if len(I):
            print('Moving', len(I), 'Gaia stars to MJD', tim.time.toMjd())
            ra,dec = radec_at_mjd(T.ra[I], T.dec[I], T.ref_epoch[I].astype(float),
                                  T.pmra[I], T.pmdec[I], T.parallax[I],
                                  tim.time.toMjd())
            T.ra [I] = ra
            T.dec[I] = dec

    tnow = Time()
    print('Read catalog:', tnow-tlast)
    tlast = tnow

    # Find SGA galaxies outside this chip and subtract them before we begin.
    chipwcs = tim.subwcs
    _,xx,yy = chipwcs.radec2pixelxy(T.ra, T.dec)
    W,H = chipwcs.get_width(), chipwcs.get_height()
    sga_out = (T.ref_cat=='L3') * np.logical_not((xx >= 1) * (xx <= W) * (yy >= 1) * (yy <= H))
    I = np.flatnonzero(sga_out)
    if len(I):
        if not 'flux_%s' % tim.band in T.get_columns():
            print('SGA galaxies outside but touching the image exist, but no flux measurements for'
                  ' band', tim.band, 'so no subtraction.')
        else:
            print(len(I), 'SGA galaxies are outside the image.  Subtracting...')
            cat = read_fits_catalog(T[I], bands=[tim.band])
            tr = Tractor([tim], cat)
            mod = tr.getModelImage(0)
            tim.data -= mod
            I = np.flatnonzero(np.logical_not(sga_out))
            T.cut(I)

    # Add in a fake flux_{BAND} column, with flux 1.0 nanomaggies
    T.set('flux_'+tim.band, np.ones(len(T), np.float32))
    cat = read_fits_catalog(T, bands=[tim.band])

    tnow = Time()
    print('Parse catalog:', tnow-tlast)
    tlast = tnow

    print('Forced photom for', im, '...')
    F = run_forced_phot(cat, tim,
                        ceres=opt.ceres,
                        derivs=opt.derivs,
                        fixed_also=True,
                        agn=opt.agn,
                        do_forced=opt.forced,
                        do_apphot=opt.apphot,
                        get_model=opt.save_model,
                        ps=ps, timing=True,
                        ceres_threads=opt.ceres_threads)

    if opt.save_model:
        # unpack results
        F,model_img = F

    F.release   = T.release
    F.brickid   = T.brickid
    F.brickname = T.brickname
    F.objid     = T.objid

    F.camera  = np.array([ccd.camera] * len(F))
    F.expnum  = np.array([im.expnum]  * len(F), dtype=np.int64)
    F.ccdname = np.array([im.ccdname] * len(F))

    # "Denormalizing"
    F.filter  = np.array([tim.band]               * len(F))
    F.mjd     = np.array([tim.primhdr['MJD-OBS']] * len(F))
    F.exptime = np.array([tim.primhdr['EXPTIME']] * len(F), dtype=np.float32)
    F.psfsize = np.array([tim.psf_fwhm * tim.imobj.pixscale] * len(F), dtype=np.float32)
    F.ccd_cuts = np.array([ccd.ccd_cuts] * len(F))
    F.airmass  = np.array([ccd.airmass ] * len(F), dtype=np.float32)
    ### --> also add units to the dict below so the FITS headers have units
    F.sky     = np.array([tim.midsky / tim.zpscale / tim.imobj.pixscale**2] * len(F), dtype=np.float32)
    # in the same units as the depth maps -- flux inverse-variance.
    F.psfdepth = np.array([(1. / (tim.sig1 / tim.psfnorm)**2)] * len(F), dtype=np.float32)
    F.galdepth = np.array([(1. / (tim.sig1 / tim.galnorm)**2)] * len(F), dtype=np.float32)
    F.fwhm     = np.array([tim.psf_fwhm] * len(F), dtype=np.float32)
    F.skyrms   = np.array([ccd.skyrms]   * len(F), dtype=np.float32)
    F.ccdzpt   = np.array([ccd.ccdzpt]   * len(F), dtype=np.float32)
    F.ccdrarms = np.array([ccd.ccdrarms] * len(F), dtype=np.float32)
    F.ccddecrms= np.array([ccd.ccddecrms]* len(F), dtype=np.float32)
    F.ccdphrms = np.array([ccd.ccdphrms] * len(F), dtype=np.float32)

    if opt.derivs:
        cosdec = np.cos(np.deg2rad(T.dec))
        with np.errstate(divide='ignore', invalid='ignore'):
            F.dra  = (F.flux_dra  / F.flux) * 3600. / cosdec
            F.ddec = (F.flux_ddec / F.flux) * 3600.
        F.dra [F.flux == 0] = 0.
        F.ddec[F.flux == 0] = 0.
        F.dra_ivar  = F.flux_dra_ivar  * (F.flux / 3600. * cosdec)**2
        F.ddec_ivar = F.flux_ddec_ivar * (F.flux / 3600.)**2
        F.delete_column('flux_dra')
        F.delete_column('flux_ddec')
        F.delete_column('flux_dra_ivar')
        F.delete_column('flux_ddec_ivar')
        F.flux = F.flux_fixed
        F.flux_ivar = F.flux_fixed_ivar
        F.delete_column('flux_fixed')
        F.delete_column('flux_fixed_ivar')

        for c in ['dra', 'ddec', 'dra_ivar', 'ddec_ivar', 'flux', 'flux_ivar']:
            F.set(c, F.get(c).astype(np.float32))

    F.ra  = T.ra
    F.dec = T.dec

    _,x,y = tim.sip_wcs.radec2pixelxy(T.ra, T.dec)
    F.x = (x-1).astype(np.float32)
    F.y = (y-1).astype(np.float32)

    h,w = tim.shape
    ix = np.round(F.x).astype(int)
    iy = np.round(F.y).astype(int)
    F.dqmask = tim.dq[np.clip(iy, 0, h-1), np.clip(ix, 0, w-1)]
    # Set an OUT-OF-BOUNDS bit.
    F.dqmask[reduce(np.logical_or, [ix < 0, ix >= w, iy < 0, iy >= h])] |= DQ_BITS['edge2']

    program_name = sys.argv[0]
    ## FIXME -- from catalog?
    release = 9999
    version_hdr = get_version_header(program_name, surveydir, release)
    filename = getattr(ccd, 'image_filename')
    if filename is None:
        # HACK -- print only two directory names + filename of CPFILE.
        fname = os.path.basename(im.imgfn.strip())
        d = os.path.dirname(im.imgfn)
        d1 = os.path.basename(d)
        d = os.path.dirname(d)
        d2 = os.path.basename(d)
        filename = os.path.join(d2, d1, fname)
        print('Trimmed filename to', filename)
    version_hdr.add_record(dict(name='CPFILE', value=filename, comment='CP file'))
    version_hdr.add_record(dict(name='CPHDU', value=im.hdu, comment='CP ext'))
    version_hdr.add_record(dict(name='CAMERA', value=ccd.camera, comment='Camera'))
    version_hdr.add_record(dict(name='EXPNUM', value=im.expnum, comment='Exposure num'))
    version_hdr.add_record(dict(name='CCDNAME', value=im.ccdname, comment='CCD name'))
    version_hdr.add_record(dict(name='FILTER', value=tim.band, comment='Bandpass of this image'))
    version_hdr.add_record(dict(name='PLVER', value=ccd.plver, comment='CP pipeline version'))
    version_hdr.add_record(dict(name='PLPROCID', value=ccd.plprocid, comment='CP pipeline id'))
    version_hdr.add_record(dict(name='PROCDATE', value=ccd.procdate, comment='CP image DATE'))

    keys = ['TELESCOP','OBSERVAT','OBS-LAT','OBS-LONG','OBS-ELEV',
            'INSTRUME']
    for key in keys:
        if key in tim.primhdr:
            version_hdr.add_record(dict(name=key, value=tim.primhdr[key]))

    if opt.save_model or opt.save_data:
        hdr = fitsio.FITSHDR()
        tim.getWcs().wcs.add_to_header(hdr)
    if opt.save_model:
        fitsio.write(opt.save_model, model_img, header=hdr, clobber=True)
        print('Wrote', opt.save_model)
    if opt.save_data:
        fitsio.write(opt.save_data, tim.getImage(), header=hdr, clobber=True)
        print('Wrote', opt.save_data)

    tnow = Time()
    print('Forced phot:', tnow-tlast)
    return F,version_hdr,outlier_mask,outlier_header

def run_forced_phot(cat, tim, ceres=True, derivs=False, agn=False,
                    do_forced=True, do_apphot=True, get_model=False, ps=None,
                    timing=False,
                    fixed_also=False,
                    ceres_threads=1):
    '''
    fixed_also: if derivs=True, also run without derivatives and report
    that flux too?
    '''
    if timing:
        tlast = Time()
    if ps is not None:
        import pylab as plt
    opti = None
    forced_kwargs = {}
    if ceres:
        from tractor.ceres_optimizer import CeresOptimizer
        B = 8

        try:
            opti = CeresOptimizer(BW=B, BH=B, threads=ceres_threads)
        except:
            if ceres_threads > 1:
                raise RuntimeError('ceres_threads requested but not supported by tractor.ceres version')
            opti = CeresOptimizer(BW=B, BH=B)
        #forced_kwargs.update(verbose=True)

    # nsize = 0
    for src in cat:
        src.freezeAllBut('brightness')
        src.getBrightness().freezeAllBut(tim.band)
    #print('Limited the size of', nsize, 'large galaxy models')

    if derivs:
        realsrcs = []
        derivsrcs = []
        Iderivs = []
        for i,src in enumerate(cat):
            from tractor import PointSource
            realsrcs.append(src)
            if not isinstance(src, PointSource):
                continue
            realmod = src.getUnitFluxModelPatch(tim)
            if realmod is None:
                continue
            Iderivs.append(i)
            brightness_dra  = src.getBrightness().copy()
            brightness_ddec = src.getBrightness().copy()
            brightness_dra .setParams(np.zeros(brightness_dra .numberOfParams()))
            brightness_ddec.setParams(np.zeros(brightness_ddec.numberOfParams()))
            brightness_dra .freezeAllBut(tim.band)
            brightness_ddec.freezeAllBut(tim.band)
            dsrc = SourceDerivatives(src, [brightness_dra, brightness_ddec], tim, ps)
            derivsrcs.append(dsrc)
        Iderivs = np.array(Iderivs)

        if fixed_also:
            pass
        else:
            # For convenience, put all the real sources at the front of
            # the list, so we can pull the IVs off the front of the list.
            cat = realsrcs + derivsrcs

    if agn:
        from tractor.galaxy import ExpGalaxy, DevGalaxy
        from tractor import PointSource
        from tractor.sersic import SersicGalaxy
        from legacypipe.survey import RexGalaxy

        realsrcs = []
        agnsrcs = []
        iagn = []
        for i,src in enumerate(cat):
            realsrcs.append(src)
            if isinstance(src, RexGalaxy):
                continue
            if isinstance(src, (ExpGalaxy, DevGalaxy, SersicGalaxy)):
                iagn.append(i)
                bright = src.getBrightness().copy()
                bright.setParams(np.zeros(bright.numberOfParams()))
                bright.freezeAllBut(tim.band)
                agn = PointSource(src.pos, bright)
                agn.freezeAllBut('brightness')
                #print('Adding "agn"', agn, 'to', src)
                #print('agn params:', agn.getParamNames())
                agnsrcs.append(src)
        iagn = np.array(iagn)
        cat = realsrcs + agnsrcs
        print('Added AGN to', len(iagn), 'galaxies')

    tr = Tractor([tim], cat, optimizer=opti)
    tr.freezeParam('images')
    disable_galaxy_cache()

    F = fits_table()

    if do_forced:

        if timing and (derivs or agn):
            t = Time()
            print('Setting up:', t-tlast)
            tlast = t

        if derivs:
            if fixed_also:
                print('Forced photom with fixed positions:')
                R = tr.optimize_forced_photometry(variance=True, fitstats=False,
                                                  shared_params=False, priors=False,
                                                  **forced_kwargs)
                F.flux_fixed = np.array([src.getBrightness().getFlux(tim.band)
                                         for src in cat]).astype(np.float32)
                N = len(cat)
                F.flux_fixed_ivar = R.IV[:N].astype(np.float32)

                if timing:
                    t = Time()
                    print('Forced photom with fixed positions finished:', t-tlast)
                    tlast = t

                cat = realsrcs + derivsrcs
                tr.setCatalog(Catalog(*cat))
            print('Forced photom with position derivatives:')

        if ps is None and not get_model:
            forced_kwargs.update(wantims=False)

        R = tr.optimize_forced_photometry(variance=True, fitstats=True,
                                          shared_params=False, priors=False,
                                          **forced_kwargs)

        if ps is not None or get_model:
            (data,mod,ie,chi,_) = R.ims1[0]

        if ps is not None:
            ima = dict(vmin=-2.*tim.sig1, vmax=5.*tim.sig1,
                       interpolation='nearest', origin='lower',
                       cmap='gray')
            imchi = dict(interpolation='nearest', origin='lower',
                         vmin=-5, vmax=5, cmap='RdBu')
            plt.clf()
            plt.imshow(data, **ima)
            plt.title('Data: %s' % tim.name)
            ps.savefig()

            plt.clf()
            plt.imshow(mod, **ima)
            plt.title('Model: %s' % tim.name)
            ps.savefig()

            plt.clf()
            plt.imshow(chi, **imchi)
            plt.title('Chi: %s' % tim.name)
            ps.savefig()

            if derivs:
                trx = Tractor([tim], realsrcs)
                trx.freezeParam('images')

                modx = trx.getModelImage(0)
                chix = (data - modx) * tim.getInvError()

                plt.clf()
                plt.imshow(modx, **ima)
                plt.title('Model without derivatives: %s' % tim.name)
                ps.savefig()

                plt.clf()
                plt.imshow(chix, **imchi)
                plt.title('Chi without derivatives: %s' % tim.name)
                ps.savefig()

        if derivs or agn:
            cat = realsrcs
        N = len(cat)

        F.flux = np.array([src.getBrightness().getFlux(tim.band)
                           for src in cat]).astype(np.float32)
        F.flux_ivar = R.IV[:N].astype(np.float32)

        F.fracflux = R.fitstats.profracflux[:N].astype(np.float32)
        F.fracin   = R.fitstats.fracin     [:N].astype(np.float32)
        F.rchisq   = R.fitstats.prochi2    [:N].astype(np.float32)
        F.fracmasked = R.fitstats.promasked[:N].astype(np.float32)

        if derivs:
            F.flux_dra  = np.zeros(len(F), np.float32)
            F.flux_ddec = np.zeros(len(F), np.float32)
            F.flux_dra [Iderivs] = np.array([src.getParams()[0]
                                             for src in derivsrcs]).astype(np.float32)
            F.flux_ddec[Iderivs] = np.array([src.getParams()[1]
                                             for src in derivsrcs]).astype(np.float32)
            F.flux_dra_ivar  = np.zeros(len(F), np.float32)
            F.flux_ddec_ivar = np.zeros(len(F), np.float32)
            F.flux_dra_ivar [Iderivs] = R.IV[N  ::2].astype(np.float32)
            F.flux_ddec_ivar[Iderivs] = R.IV[N+1::2].astype(np.float32)

        if agn:
            F.flux_agn = np.zeros(len(F), np.float32)
            F.flux_agn_ivar = np.zeros(len(F), np.float32)
            if len(iagn):
                F.flux_agn[iagn] = np.array([src.getParams()[0] for src in agnsrcs])
                F.flux_agn_ivar[iagn] = R.IV[N:].astype(np.float32)

        if timing:
            t = Time()
            print('Forced photom:', t-tlast)
            tlast = t

    if do_apphot:
        import photutils

        img = tim.getImage()
        ie = tim.getInvError()
        with np.errstate(divide='ignore'):
            imsigma = 1. / ie
        imsigma[ie == 0] = 0.

        apimg = []
        apimgerr = []

        # Aperture photometry locations -- this is using the Tractor wcs infrastructure,
        # so pixel positions are 0-indexed.
        apxy = np.vstack([tim.wcs.positionToPixel(src.getPosition()) for src in cat])

        apertures = apertures_arcsec / tim.wcs.pixel_scale()

        # The aperture photometry routine doesn't like pixel positions outside the image
        H,W = img.shape
        Iap = np.flatnonzero((apxy[:,0] >= 0)   * (apxy[:,1] >= 0) *
                             (apxy[:,0] <= W-1) * (apxy[:,1] <= H-1))
        print('Aperture photometry for', len(Iap), 'of', len(apxy[:,0]), 'sources within image bounds')

        for rad in apertures:
            aper = photutils.CircularAperture(apxy[Iap,:], rad)
            p = photutils.aperture_photometry(img, aper, error=imsigma)
            apimg.append(p.field('aperture_sum'))
            apimgerr.append(p.field('aperture_sum_err'))
        ap = np.vstack(apimg).T
        ap[np.logical_not(np.isfinite(ap))] = 0.
        F.apflux = np.zeros((len(F), len(apertures)), np.float32)
        F.apflux[Iap,:] = ap.astype(np.float32)

        apimgerr = np.vstack(apimgerr).T
        apiv = np.zeros(apimgerr.shape, np.float32)
        apiv[apimgerr != 0] = 1./apimgerr[apimgerr != 0]**2
        F.apflux_ivar = np.zeros((len(F), len(apertures)), np.float32)
        F.apflux_ivar[Iap,:] = apiv
        if timing:
            print('Aperture photom:', Time()-tlast)

    if get_model:
        return F,mod
    return F

from tractor import MultiParams, BasicSource
class SourceDerivatives(MultiParams, BasicSource):
    def __init__(self, real, brights, tim, ps):
        from tractor.patch import Patch
        '''
        *real*: The real source whose derivatives are my profiles.
        '''
        # This a subclass of MultiParams and we pass the brightnesses
        # as our params.
        super(SourceDerivatives,self).__init__(*brights)
        self.real = real
        self.brights = brights
        self.umods = None

        # Get the current source profile and take pixel-space
        # derivatives by hand, for speed and to get symmetric
        # derivatives.
        realmod = real.getUnitFluxModelPatch(tim)
        p = realmod.patch
        dx = np.zeros_like(p)
        dy = np.zeros_like(p)
        # Omit a boundary of 1 pixel on all sides in both derivatives
        dx[1:-1, 1:-1] = (p[1:-1, :-2] - p[1:-1, 2:]) / 2.
        dy[1:-1, 1:-1] = (p[:-2, 1:-1] - p[2:, 1:-1]) / 2.
        # Convert from pixel-space to RA,Dec derivatives via CD matrix
        px, py = tim.getWcs().positionToPixel(real.pos, real)
        cdi = tim.getWcs().cdInverseAtPixel(px, py)
        dra  = dx * cdi[0, 0] + dy * cdi[1, 0]
        ddec = dx * cdi[0, 1] + dy * cdi[1, 1]

        if ps is not None:
            import pylab as plt
            mx = p.max()
            plt.clf()
            plt.subplot(2,3,1)
            plt.imshow(p, interpolation='nearest', origin='lower',
                       vmin=0, vmax=mx)
            mx *= 0.25
            plt.subplot(2,3,2)
            plt.imshow(dx, interpolation='nearest', origin='lower',
                       vmin=-mx, vmax=mx)
            plt.title('dx')
            plt.subplot(2,3,3)
            plt.imshow(dy, interpolation='nearest', origin='lower',
                       vmin=-mx, vmax=mx)
            plt.title('dy')
            plt.subplot(2,3,5)
            sc = 3600/0.262
            plt.imshow(dra/sc, interpolation='nearest', origin='lower',
                       vmin=-mx, vmax=mx)
            plt.title('dra')
            plt.subplot(2,3,6)
            plt.imshow(ddec/sc, interpolation='nearest', origin='lower',
                       vmin=-mx, vmax=mx)
            plt.title('ddec')
            ps.savefig()
        # These are our "unit-flux" models
        self.umods = [Patch(realmod.x0, realmod.y0, dra),
                      Patch(realmod.x0, realmod.y0, ddec)]

    @staticmethod
    def getNamedParams():
        return dict(dra=0, ddec=1)

    # forced photom calls getUnitFluxModelPatches
    def getUnitFluxModelPatches(self, img, modelMask=None, minval=None):
        return self.umods

    def getModelPatch(self, img, minsb=0., modelMask=None):
        from tractor.patch import add_patches
        pc = img.getPhotoCal()
        p1 = self.umods[0] * pc.brightnessToCounts(self.brights[0])
        p2 = self.umods[1] * pc.brightnessToCounts(self.brights[1])
        return add_patches(p1, p2)

if __name__ == '__main__':
    from astrometry.util.ttime import MemMeas
    Time.add_measurement(MemMeas)
    sys.exit(main())
