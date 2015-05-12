#!/usr/bin/env python
from collections import OrderedDict as odict
import copy

import numpy
import numpy as np
from scipy.stats import norm

import healpy
import healpy as hp
import pyfits

import ugali.utils.binning
import ugali.utils.parabola

from ugali.utils.projector import angsep, gal2cel
from ugali.utils.healpix import ang2pix,pix2ang
from ugali.utils.logger import logger
from ugali.analysis.model import Model,Parameter

class Richness(Model):
    """
    Dummy model to hold the richness, which is not
    directly connected to either the spatial or 
    color information and doesn't require a sync
    when updated.
    """
    _params = odict([
        ('richness', Parameter(1.0, [0.0,  np.inf])),
    ])

class LogLikelihood(object):
    """
    Class for calculating the log-likelihood from a set of models.
    """

    def __init__(self, config, roi, mask, catalog, isochrone, kernel):
        # Set the various models for the likelihood
        self.set_model('richness', Richness())
        self.set_model('color', isochrone)
        self.set_model('spatial', kernel) 

        # Toggle for tracking which models need to be synched
        self._sync = odict([(k,True) for k in self.models.keys()])

        # Currently assuming that input mask is ROI-specific
        self.config = config
        self.roi    = roi
        self.mask   = mask

        self.catalog_full = catalog
        self.clip_catalog()

        # Effective bin size in color-magnitude space
        self.delta_mag = self.config['likelihood']['delta_mag']

        self.spatial_only = self.config['likelihood'].get('spatial_only',False)
        self.color_only   = self.config['likelihood'].get('color_only',False)

        if self.spatial_only and self.color_only:
            msg = "Both 'spatial_only' and 'color_only' set"
            logger.error(msg)
            raise ValueError(msg)
        elif self.spatial_only: 
            logger.warning("Likelihood calculated from spatial information only!!!")
        elif self.color_only:
            logger.warning("Likelihood calculated from color information only!!!")

        self.calc_background()

    def __call__(self):
        # The signal probability for each object
        #self.p = (self.richness * self.u) / ((self.richness * self.u) + self.b)
        # The total model predicted counts
        return -1. * numpy.sum(numpy.log(1. - self.p)) - (self.f * self.richness)
        
    def __getattr__(self, name):
        for key,model in self.models.items():
            if name in model.params:
                return getattr(model, name)
        # Raises AttributeError
        return object.__getattribute__(self,name)

    def __setattr__(self, name, value):
        for key,model in self.models.items():
            if name in model.params:
                self._sync[key] = True
                return setattr(model, name, value)
        # Raises AttributeError?
        return object.__setattr__(self, name, value)
        
    def __str__(self):
        ret = "%s:"%self.__class__.__name__
        for key,model in self.models.items():
            ret += "\n  %s Model (sync=%s):\n"%(key.capitalize(),self._sync[key])
            ret += model.__str__(indent=4)
        return ret

    ############################################################################
    # Derived and protected properties
    ############################################################################

    # Various derived properties
    @property
    def kernel(self):
        return self.models['spatial']

    @property
    def isochrone(self):
        return self.models['color']

    @property
    def params(self):
        params = odict([])
        for key,model in self.models.items():
            params.update(model.params)
        return params

    @property
    def pixel(self):
        nside = self.config.params['coords']['nside_pixel']
        pixel = ang2pix(nside,float(self.lon),float(self.lat))
        return pixel

    # Protect the basic elements of the likelihood
    @property
    def u(self):
        return self._u
    @property
    def b(self):
        return self._b
    @property
    def p(self):
        return self._p
    @property
    def f(self):
        return self._f
        
    def value(self,**kwargs):
        """
        Evaluate the log-likelihood at the given input parameter values
        """
        self.set_params(**kwargs)
        self.sync_params()
        return self()

    ############################################################################
    # Methods for setting model parameters
    ############################################################################

    def set_model(self, name, model):
        if not hasattr(self,'models'):
            object.__setattr__(self, 'models',odict())
        #ADW: Might want to consider this 
        ###if name not in self.models:
        ###    msg="%s does not contain model: %s"%(self.__class__.__name__,name)
        ###    raise AttributeError(msg)
        self.models[name] = model

    def set_params(self,**kwargs):
        for key,value in kwargs.items():
            setattr(self,key,value)

        if self.pixel not in self.roi.pixels_interior:
            # ADW: Raising this exception is not strictly necessary, 
            # but at least a warning should be printed if target outside of region.
            raise ValueError("Coordinate outside interior ROI.")

    def sync_params(self):
        # The sync_params step updates internal quantities based on
        # newly set parameters. The goal is to only update required quantities
        # to keep computational time low.
 
        if self._sync['richness']:
            # No sync necessary for richness
            pass
        if self._sync['color']:
            self.observable_fraction = self.calc_observable_fraction(self.distance_modulus)
            self.u_color = self.calc_signal_color(self.distance_modulus)
        if self._sync['spatial']:
            self.u_spatial = self.calc_signal_spatial()

        # Combined object-by-object signal probability
        self._u = self.u_spatial * self.u_color
         
        # Observable fraction requires update if isochrone changed
        # Fraction calculated over interior region
        self._f = self.roi.area_pixel * \
            (self.surface_intensity_sparse*self.observable_fraction).sum()

        ## ADW: Spatial information only (remember mag binning in calc_background)
        if self.spatial_only:
            self._u = self.u_spatial
            observable_fraction = (self.observable_fraction > 0)
            self._f = self.roi.area_pixel * \
                     (self.surface_intensity_sparse*observable_fraction).sum()

        # The signal probability for each object
        self._p = (self.richness * self.u) / ((self.richness * self.u) + self.b)

        #print 'b',np.unique(self.b)[:20]
        #print 'u',np.unique(self.u)[:20]
        #print 'f',np.unique(self.f)[:20]
        #print 'p',np.unique(self.p)[:20] 

        # Reset the sync toggle
        for k in self._sync.keys(): self._sync[k]=False 

    ############################################################################
    # Methods for calculating observation quantities
    ############################################################################

    def clip_catalog(self):
        # ROI-specific catalog
        logger.debug("Clipping full catalog...")
        cut_observable = self.mask.restrictCatalogToObservableSpace(self.catalog_full)

        # All objects within disk ROI
        logger.debug("Creating roi catalog...")
        self.catalog_roi = self.catalog_full.applyCut(cut_observable)
        self.catalog_roi.project(self.roi.projector)
        self.catalog_roi.spatialBin(self.roi)

        # All objects interior to the background annulus
        logger.debug("Creating interior catalog...")
        cut_interior = numpy.in1d(ang2pix(self.config['coords']['nside_pixel'], self.catalog_roi.lon, self.catalog_roi.lat), 
                                  self.roi.pixels_interior)
        #cut_interior = self.roi.inInterior(self.catalog_roi.lon,self.catalog_roi.lat)
        self.catalog_interior = self.catalog_roi.applyCut(cut_interior)
        self.catalog_interior.project(self.roi.projector)
        self.catalog_interior.spatialBin(self.roi)

        # Set the default catalog
        #logger.info("Using interior ROI for likelihood calculation")
        self.catalog = self.catalog_interior
        #self.pixel_roi_cut = self.roi.pixel_interior_cut

    def stellar_mass(self):
        # ADW: I think it makes more sense for this to be
        #return self.richness * self.isochrone.stellarMass()
        return self.isochrone.stellarMass()

    def stellar_luminosity(self):
        # ADW: I think it makes more sense for this to be
        #return self.richness * self.isochrone.stellarLuminosity()
        return self.isochrone.stellarLuminosity()

    def absolute_magnitude(self):
        # ADW: I think it makes more sense for this to be
        #return 4.85 - 2.5*np.log10(self.stellar_luminosity()))
        return 4.85 - 2.5*np.log10(self.richness*self.stellar_luminosity())

    def calc_backgroundCMD(self):
        #ADW: At some point we may want to make the background level a fit parameter.
        logger.info('Calculating background CMD ...')
        self.cmd_background = self.mask.backgroundCMD(self.catalog_roi)
        #self.cmd_background = self.mask.backgroundCMD(self.catalog_roi,mode='histogram')
        #self.cmd_background = self.mask.backgroundCMD(self.catalog_roi,mode='uniform')
        # Background density (deg^-2 mag^-2) and background probability for each object
        logger.info('Calculating background probabilities ...')
        b_density = ugali.utils.binning.take2D(self.cmd_background,
                                               self.catalog.color, self.catalog.mag,
                                               self.roi.bins_color, self.roi.bins_mag)

        # ADW: I don't think this 'area_pixel' or 'delta_mag' factors are necessary, 
        # so long as it is also removed from u_spatial and u_color
        #self._b = b_density * self.roi.area_pixel * self.delta_mag**2
        self._b = b_density

        if self.spatial_only:
            # ADW: This assumes a flat mask...
            solid_angle_annulus = (self.mask.mask_1.mask_annulus_sparse > 0).sum()*self.roi.area_pixel
            b_density = self.roi.inAnnulus(self.catalog_roi.lon,self.catalog_roi.lat).sum()/solid_angle_annulus
            self._b = np.array([b_density*self.roi.area_pixel])

    def calc_backgroundMMD(self):
        #ADW: At some point we may want to make the background level a fit parameter.
        logger.info('Calculating background MMD ...')
        self.mmd_background = self.mask.backgroundMMD(self.catalog_roi)
        #self.mmd_background = self.mask.backgroundMMD(self.catalog_roi,mode='histogram')
        #self.mmd_background = self.mask.backgroundMMD(self.catalog_roi,mode='uniform')
        # Background density (deg^-2 mag^-2) and background probability for each object
        logger.info('Calculating background probabilities ...')
        b_density = ugali.utils.binning.take2D(self.mmd_background,
                                               self.catalog.mag_2, self.catalog.mag_1,
                                               self.roi.bins_mag, self.roi.bins_mag)

        # ADW: I don't think this 'area_pixel' or 'delta_mag' factors are necessary, 
        # so long as it is also removed from u_spatial and u_color
        #self._b = b_density * self.roi.area_pixel * self.delta_mag**2
        self._b = b_density

        if self.spatial_only:
            # ADW: This assumes a flat mask...
            solid_angle_annulus = (self.mask.mask_1.mask_annulus_sparse > 0).sum()*self.roi.area_pixel
            b_density = self.roi.inAnnulus(self.catalog_roi.lon,self.catalog_roi.lat).sum()/solid_angle_annulus
            self._b = np.array([b_density*self.roi.area_pixel])

    # FIXME: Need to parallelize CMD and MMD formulation
    calc_background = calc_backgroundCMD

    def calc_observable_fraction(self,distance_modulus):
        """
        Calculated observable fraction within each pixel of the target region.
        """
        # This is the observable fraction after magnitude cuts in each 
        # pixel of the ROI.
        observable_fraction = self.isochrone.observableFraction(self.mask,distance_modulus)
        if not observable_fraction.sum() > 0:
            msg = "No observable fraction"
            logger.error(msg)
            raise ValueError(msg)
        return observable_fraction

    def calc_signal_color1(self, distance_modulus, mass_steps=10000):
        """
        Compute signal color probability (u_color) for each catalog object on the fly.
        """
        mag_1, mag_2 = self.catalog.mag_1,self.catalog.mag_2
        mag_err_1, mag_err_2 = self.catalog.mag_err_1,self.catalog.mag_err_2
        u_density = self.isochrone.pdf(mag_1,mag_2,mag_err_1,mag_err_2,distance_modulus,self.delta_mag,mass_steps)

        #u_color = u_density * self.delta_mag**2
        u_color = u_density

        return u_color

    def calc_signal_color2(self, distance_modulus, mass_steps=1000):
        """
        Compute signal color probability (u_color) for each catalog object on the fly.
        """
        logger.info('Calculating signal color from MMD')

        mag_1, mag_2 = self.catalog.mag_1,self.catalog.mag_2
        lon, lat = self.catalog.lon,self.catalog.lat
        u_density = self.isochrone.pdf_mmd(lon,lat,mag_1,mag_2,distance_modulus,self.mask,self.delta_mag,mass_steps)

        #u_color = u_density * self.delta_mag**2
        u_color = u_density

        # ADW: Should calculate observable fraction here as well...

        return u_color

    # FIXME: Need to parallelize CMD and MMD formulation
    calc_signal_color = calc_signal_color1

    def calc_signal_spatial(self):
        # At the pixel level over the ROI
        pix_lon,pix_lat = self.roi.pixels_interior.lon,self.roi.pixels_interior.lat

        #self.angsep_sparse = angsep(self.lon,self.lat,pix_lon,pix_lat)
        #self.surface_intensity_sparse = self.kernel.surfaceIntensity(self.angsep_sparse)
        self.surface_intensity_sparse = self.kernel.pdf(pix_lon,pix_lat)

        # On the object-by-object level
        #self.angsep_object = angsep(self.lon,self.lat,self.catalog.lon,self.catalog.lat)
        #self.surface_intensity_object = self.kernel.surfaceIntensity(self.angsep_object)
        self.surface_intensity_object = self.kernel.pdf(self.catalog.lon,self.catalog.lat)
        
        # Spatial component of signal probability
        #u_spatial = self.roi.area_pixel * self.surface_intensity_object
        u_spatial = self.surface_intensity_object
        return u_spatial

    ############################################################################
    # Methods for fitting and working with the likelihood
    ############################################################################

    def fit_richness(self, atol=1.e-3, maxiter=50):
        """
        Maximize the log-likelihood as a function of richness.
        """
        # Check whether the signal probability for all objects are zero
        # This can occur for finite kernels on the edge of the survey footprint
        if numpy.isnan(self.u).any():
            logger.warning("NaN signal probability found")
            return 0., 0., None
        
        if not numpy.any(self.u):
            logger.warning("Signal probability is zero for all objects")
            return 0., 0., None

        # Richness corresponding to 0, 1, and 10 observable stars
        richness = np.array([0., 1./self.f, 10./self.f])
        loglike = []
        for r in richness:
            loglike.append(self.value(richness=r))
        loglike = np.array(loglike)

        found_maximum = False
        iteration = 0
        while not found_maximum:
            parabola = ugali.utils.parabola.Parabola(richness, 2.*loglike)
            if parabola.vertex_x < 0.:
                found_maximum = True
            else:
                richness = numpy.append(richness, parabola.vertex_x)
                loglike  = numpy.append(loglike, self.value(richness=richness[-1]))    

                if numpy.fabs(loglike[-1] - numpy.max(loglike[0: -1])) < atol:
                    found_maximum = True
            iteration+=1
            if iteration > maxiter:
                logger.warning("Maximum number of iterations reached")
                break
            
        index = numpy.argmax(loglike)
        return loglike[index], richness[index], parabola

    def richness_interval(self, alpha=0.6827, n_pdf_points=100):
        loglike_max, richness_max, parabola = self.fit_richness()

        richness_range = parabola.profileUpperLimit(delta=25.) - richness_max
        richness = numpy.linspace(max(0., richness_max - richness_range),
                                  richness_max + richness_range,
                                  n_pdf_points)
        if richness[0] > 0.:
            richness = numpy.insert(richness, 0, 0.)
            n_pdf_points += 1
        
        log_likelihood = numpy.zeros(n_pdf_points)
        for kk in range(0, n_pdf_points):
            log_likelihood[kk] = self.value(richness=richness[kk])
        parabola = ugali.utils.parabola.Parabola(richness, 2.*log_likelihood)
        return parabola.confidenceInterval(alpha)


    def write_membership(self,filename):
        ra,dec = gal2cel(self.catalog.lon,self.catalog.lat)
        
        name_objid = self.config['catalog']['objid_field']
        name_mag_1 = self.config['catalog']['mag_1_field']
        name_mag_2 = self.config['catalog']['mag_2_field']
        name_mag_err_1 = self.config['catalog']['mag_err_1_field']
        name_mag_err_2 = self.config['catalog']['mag_err_2_field']

        columns = [
            pyfits.Column(name=name_objid,format='K',array=self.catalog.objid),
            pyfits.Column(name='GLON',format='D',array=self.catalog.lon),
            pyfits.Column(name='GLAT',format='D',array=self.catalog.lat),
            pyfits.Column(name='RA',format='D',array=ra),
            pyfits.Column(name='DEC',format='D',array=dec),
            pyfits.Column(name=name_mag_1,format='E',array=self.catalog.mag_1),
            pyfits.Column(name=name_mag_err_1,format='E',array=self.catalog.mag_err_1),
            pyfits.Column(name=name_mag_2,format='E',array=self.catalog.mag_2),
            pyfits.Column(name=name_mag_err_2,format='E',array=self.catalog.mag_err_2),
            pyfits.Column(name='COLOR',format='E',array=self.catalog.color),
            pyfits.Column(name='PROB',format='E',array=self.p),
        ]
        hdu = pyfits.new_table(columns)
        for param,value in self.params.items():
            # HIERARCH allows header keywords longer than 8 characters
            name = 'HIERARCH %s'%param.upper()
            hdu.header.set(name,value.value,param)
        hdu.writeto(filename,clobber=True)


def createROI(config,lon,lat):
    import ugali.observation.roi
    roi = ugali.observation.roi.ROI(config, lon, lat)        
    return roi

def createKernel(config,lon=0.0,lat=0.0):
    import ugali.analysis.kernel
    params = dict(config['kernel'])
    params.setdefault('lon',lon)
    params.setdefault('lat',lat)
    return ugali.analysis.kernel.kernelFactory(**params)

def createIsochrone(config):
    #logger.warning("Creating old isochrone...")
    #import ugali.analysis.isochrone
    #isochrone = ugali.analysis.isochrone.CompositeIsochrone(config)
    logger.warning("Creating new isochrone...")
    import ugali.analysis.isochrone2
    isochrone = ugali.analysis.isochrone2.isochroneFactory(**config['isochrone'])
    return isochrone

def createCatalog(config,roi=None,lon=None,lat=None):
    """
    Create a catalog object
    """
    import ugali.observation.catalog
    if roi is None: roi = createROI(config,lon,lat)
    catalog = ugali.observation.catalog.Catalog(config,roi=roi)  
    return catalog

def simulateCatalog(config,roi=None,lon=None,lat=None):
    """
    Simulate a catalog object.
    """
    import ugali.simulation.simulator
    if roi is None: roi = createROI(config,lon,lat)
    sim = ugali.simulation.simulator.Simulator(config,roi)
    return sim.catalog()

def createMask(config,roi=None,lon=None,lat=None):
    import ugali.observation.mask
    if roi is None: 
        if lon is None or lat is None: 
            msg = "Without `roi`, `lon` and `lat` must be specified"
            raise Exception(msg)
        roi = createROI(config,lon,lat)
    mask = ugali.observation.mask.Mask(config, roi)
    return mask

def createLoglike(config,lon,lat):
    roi = createROI(config,lon,lat)
    kernel = createKernel(config,lon,lat)
    isochrone = createIsochrone(config)
    catalog = createCatalog(config,roi)
    mask = createMask(config,roi)
    return LogLikelihood(config,roi,mask,catalog,isochrone,kernel)

if __name__ == "__main__":
    import argparse
    description = "python script"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('args',nargs=argparse.REMAINDER)
    opts = parser.parse_args(); args = opts.args
