from __future__ import print_function
import warnings
import numpy as np

from legacypipe.image import LegacySurveyImage, validate_version
from legacypipe.utils import read_primary_header

import logging
logger = logging.getLogger('legacypipe.decam')
def info(*args):
    from legacypipe.utils import log_info
    log_info(logger, args)
def debug(*args):
    from legacypipe.utils import log_debug
    log_debug(logger, args)

'''
Code specific to images from the Dark Energy Camera (DECam).
'''

class DecamImage(LegacySurveyImage):
    '''
    A LegacySurveyImage subclass to handle images from the Dark Energy
    Camera, DECam, on the Blanco telescope.
    '''
    def __init__(self, survey, t, image_fn=None, image_hdu=0):
        super(DecamImage, self).__init__(survey, t, image_fn=image_fn, image_hdu=image_hdu)
        # Adjust zeropoint for exposure time
        self.ccdzpt += 2.5 * np.log10(self.exptime)

        # Nominal zeropoints
        # These are used only for "ccdskybr", so are not critical.
        self.zp0 =  dict(g = 25.001,
                         r = 25.209,
                         z = 24.875,
                         # u from Arjun 2021-03-17, based on DECosmos-to-SDSS
                         u = 23.3205,
                         # i,Y from DESY1_Stripe82 95th percentiles
                         i = 25.149,
                         Y = 23.712,
                         N501 = 23.812,
                         N673 = 24.151,
        )
        # extinction per airmass, from Arjun's 2019-02-27
        # "Zeropoint variations with MJD for DECam data".
        self.k_ext = dict(g = 0.173,
                          r = 0.090,
                          i = 0.054,
                          z = 0.060,
                          Y = 0.058,
                          # From Arjun 2021-03-17 based on DECosmos (calib against SDSS)
                          u = 0.63,
            )

    @classmethod
    def get_nominal_pixscale(cls):
        return 0.262

    def get_site(self):
        from astropy.coordinates import EarthLocation
        # zomg astropy's caching mechanism is horrific
        # return EarthLocation.of_site('ctio')
        from astropy.units import m
        from astropy.utils import iers
        iers.conf.auto_download = False
        return EarthLocation(1814304. * m, -5214366. * m, -3187341. * m)

    def calibration_good(self, primhdr):
        '''Did the CP processing succeed for this image?  If not, no need to process further.
        '''
        wcscal = primhdr.get('WCSCAL', '').strip().lower()
        if wcscal.startswith('success'):  # success / successful
            return True
        # Frank's work-around for some with incorrect WCSCAL=Failed (DR9 re-reductions)
        return primhdr.get('SCAMPFLG') == 0

    def colorterm_sdss_to_observed(self, sdssstars, band):
        from legacypipe.ps1cat import sdss_to_decam
        return sdss_to_decam(sdssstars, band)
    def colorterm_ps1_to_observed(self, cat, band):
        from legacypipe.ps1cat import ps1_to_decam
        return ps1_to_decam(cat, band)

    def get_gain(self, primhdr, hdr):
        return np.average((hdr['GAINA'],hdr['GAINB']))

    def get_sky_template_filename(self, old_calibs_ok=False):
        import os
        from astrometry.util.fits import fits_table
        dirnm = os.environ.get('SKY_TEMPLATE_DIR', None)
        if dirnm is None:
            warnings.warn('decam: no SKY_TEMPLATE_DIR environment variable set.')
            return None
        '''
        # Create this sky-scales.kd.fits file via:
        python legacypipe/create-sky-template-kdtree.py skyscales_ccds.fits \
        sky-scales.kd.fits
        '''
        fn = os.path.join(dirnm, 'sky-scales.kd.fits')
        if not os.path.exists(fn):
            warnings.warn('decam: no $SKY_TEMPLATE_DIR/sky-scales.kd.fits file.')
            return None
        from astrometry.libkd.spherematch import tree_open
        kd = tree_open(fn, 'expnum')
        I = kd.search(np.array([self.expnum]), 0.5, 0, 0)
        if len(I) == 0:
            warnings.warn('decam: expnum %i not found in file %s' % (self.expnum, fn))
            return None
        # Read only the CCD-table rows within range.
        S = fits_table(fn, rows=I)
        S.cut(np.array([c.strip() == self.ccdname for c in S.ccdname]))
        if len(S) == 0:
            warnings.warn('decam: ccdname %s, expnum %i not found in file %s' %
                  (self.ccdname, self.expnum, fn))
            return None
        assert(len(S) == 1)
        sky = S[0]
        if sky.run == -1:
            debug('sky template: run=-1 for expnum %i, ccdname %s' % (self.expnum, self.ccdname))
            return None
        # Check PLPROCID only
        if not validate_version(
                fn, 'table', self.expnum, None, self.plprocid, data=S):
            txt = ('Sky template for expnum=%i, ccdname=%s did not pass consistency validation (EXPNUM, PLPROCID) -- image %i,%s vs template table %i,%s' %
                   (self.expnum, self.ccdname, self.expnum, self.plprocid, sky.expnum, sky.plprocid))
            if old_calibs_ok:
                info('Warning:', txt, '-- but old_calibs_ok')
            else:
                raise RuntimeError(txt)
        assert(self.band == sky.filter)
        tfn = os.path.join(dirnm, 'sky_templates',
                           'sky_template_%s_%i.fits.fz' % (self.band, sky.run))
        if not os.path.exists(tfn):
            warnings.warn('WARNING: Sky template file %s does not exist' % tfn)
            return None
        return dict(template_filename=tfn, sky_template_dir=dirnm, sky_obj=sky, skyscales_fn=fn)

    def get_sky_template(self, slc=None, old_calibs_ok=False):
        import fitsio
        d = self.get_sky_template_filename(old_calibs_ok=old_calibs_ok)
        if d is None:
            return None
        skyscales_fn = d['skyscales_fn']
        sky_template_dir = d['sky_template_dir']
        tfn = d['template_filename']
        sky = d['sky_obj']
        F = fitsio.FITS(tfn)
        f = F[self.ccdname]
        if slc is None:
            template = f.read()
        else:
            template = f[slc]
        hdr = F[0].read_header()
        ver = hdr.get('SKYTMPL', -1)
        meta = dict(sky_scales_fn=skyscales_fn, template_fn=tfn, sky_template_dir=sky_template_dir,
                    run=sky.run, scale=sky.skyscale, version=ver)
        return template * sky.skyscale, meta

    def get_good_image_subregion(self):
        x0,x1,y0,y1 = None,None,None,None

        glow_expnum = 298251

        # Handle 'glowing' edges in early r-band images
        if self.band == 'r' and self.expnum < glow_expnum:
            # Northern chips: drop 100 pix off the bottom
            if 'N' in self.ccdname:
                debug('Clipping bottom part of northern DES r-band chip')
                y0 = 100
            else:
                # Southern chips: drop 100 pix off the top
                debug('Clipping top part of southern DES r-band chip')
                y1 = self.height - 100

        # Clip the bad half of chip S7.
        # The left half is OK.
        if self.ccdname == 'S7':
            debug('Clipping the right half of chip S7')
            x1 = 1023

        return x0,x1,y0,y1

    def remap_dq(self, dq, header):
        '''
        Called by get_tractor_image() to map the results from read_dq
        into a bitmask.
        '''
        primhdr = read_primary_header(self.dqfn)
        plver = primhdr['PLVER']
        if decam_has_dq_codes(plver):
            from legacypipe.image import remap_dq_cp_codes
            # Always ignore the satellite masker code.
            ignore = [8]
            if not decam_use_dq_cr(plver):
                # In some runs of the pipeline (not captured by plver)
                # the CR and Satellite masker codes got confused, so ignore
                # both.
                ignore.append(5)
            dq = remap_dq_cp_codes(dq, ignore_codes=ignore)
        else:
            from legacypipe.bits import DQ_BITS
            dq = dq.astype(np.int16)
            # Un-set the SATUR flag for pixels that also have BADPIX set.
            bothbits = DQ_BITS['badpix'] | DQ_BITS['satur']
            I = np.flatnonzero((dq & bothbits) == bothbits)
            if len(I):
                debug('Warning: un-setting SATUR for', len(I),
                      'pixels with SATUR and BADPIX set.')
                dq.flat[I] &= ~DQ_BITS['satur']
                assert(np.all((dq & bothbits) != bothbits))
        return dq

    def fix_saturation(self, img, dq, invvar, primhdr, imghdr, slc):
        plver = primhdr['PLVER']
        if decam_s19_satur_ok(plver):
            return
        if self.ccdname != 'S19':
            return
        I,J = np.nonzero((img > 46000) * (dq == 0) * (invvar > 0))
        info('Masking %i additional saturated pixels in DECam expnum %i S19 CCD, %s' %
             (len(I), self.expnum, self.print_imgpath), 'slice', slc)
        if len(I) == 0:
            return
        from legacypipe.bits import DQ_BITS
        if dq is not None:
            dq[I,J] |= DQ_BITS['satur']
        invvar[I,J] = 0.

def decam_cp_version_after(plver, after):
    from distutils.version import StrictVersion
    # The format of the DQ maps changed as of version 3.5.0 of the
    # Community Pipeline.  Handle that here...
    plver = plver.strip()
    plver = plver.replace('V','')
    plver = plver.replace('DES ', '')
    plver = plver.replace('+1', 'a1')
    #'4.8.2a'
    if plver.endswith('2a'):
        plver = plver.replace('.2a', '.2a1')
    return StrictVersion(plver) >= StrictVersion(after)

def decam_s19_satur_ok(plver):
    return decam_cp_version_after(plver, '4.9.0')

def decam_use_dq_cr(plver):
    return decam_cp_version_after(plver, '4.8.0')

def decam_has_dq_codes(plver):
    # The format of the DQ maps changed as of version 3.5.0 of the CP
    return decam_cp_version_after(plver, '3.5.0')
