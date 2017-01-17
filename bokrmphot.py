#!/usr/bin/env python

import os
import numpy as np
import multiprocessing
from functools import partial
from astropy.io import fits
from astropy.table import Table,vstack
from astropy.stats import sigma_clip
from astropy.wcs import InconsistentAxisTypesError

from bokpipe import bokphot,bokpl,bokproc,bokutil
import bokrmpipe
#from bokrmgnostic import srcor

def aper_worker(dataMap,inputType,aperRad,refCat,catDir,catPfx,
                inp,**kwargs):
	utd,filt = inp
	redo = kwargs.pop('redo',False)
	fn = '.'.join([catPfx,utd,filt,'cat','fits'])
	catFile = os.path.join(catDir,fn)
	if os.path.exists(catFile) and not redo:
		print catFile,' already exists, skipping'
		return
	files,frames = dataMap.getFiles(imType='object',utd=utd,filt=filt,
	                                with_frames=True)
	if files is None:
		return
	#
	bpMask = dataMap.getCalMap('badpix4')
	diagfile = os.path.join(dataMap.getDiagDir(), 'gainbal_%s.npz'%utd)
	gainDat = np.load(diagfile)
	#
	allPhot = []
	for f,frame in zip(files,frames):
		imageFile = dataMap(inputType)(f)
		aHeadFile = imageFile.replace('.fits','.ahead')
		fileNum = np.where(gainDat['files']==os.path.basename(f))[0][0]
		gains = gainDat['gainCor'][fileNum]
		gains = np.product(gains.squeeze(),axis=1) 
		gains *= np.array(bokproc.nominal_gain)
		skyAdu = gainDat['skys'][fileNum]
		expTime = fits.getheader(imageFile,0)['EXPTIME']
		varIm = bokpl.make_variance_image(dataMap,f,bpMask,
		                                  expTime,gains,skyAdu)
		print 'aperture photometering ',imageFile
		try:
			phot = bokphot.aper_phot_image(imageFile,
			                               refCat['ra'],refCat['dec'],
			                               aperRad,bpMask(f),varIm,
			                               aHeadFile=aHeadFile,
			                               **kwargs)
		except InconsistentAxisTypesError:
			print 'WCS FAILED!!!'
			continue
		if phot is None:
			print 'no apertures found!!!!'
			continue
		phot['frameIndex'] = dataMap.obsDb['frameIndex'][frame]
		allPhot.append(phot)
	allPhot = vstack(allPhot)
	allPhot.write(catFile,overwrite=True)

def aperture_phot(dataMap,refCat,procmap,inputType='sky',**kwargs):
	kwargs.setdefault('mask_is_weight_map',False)
	kwargs.setdefault('background','global')
	aperRad = np.concatenate([np.arange(2,9.51,1.5),[15.,22.5]])
	catDir = os.path.join(dataMap.procDir,'catalogs')
	if not os.path.exists(catDir):
		os.mkdir(catDir)
	catPfx = refCat['filePrefix']
	refCat = refCat['catalog']
	utdlist = [ (utd,filt) for utd in dataMap.iterUtDates() 
	                         for filt in dataMap.iterFilters() ]
	p_aper_worker = partial(aper_worker,dataMap,inputType,
	                        aperRad,refCat,catDir,catPfx,**kwargs)
	procmap(p_aper_worker,utdlist)

def zero_points(dataMap,magRange=(16.,19.5),aperNum=-2):
	pfx = 'bokrm_sdss'
	aperCatDir = os.path.join(dataMap.procDir,'catalogs')
	sdss = fits.getdata(os.environ['BOK90PRIMEDIR']+'/../data/sdss.fits',1)
	for filt in dataMap.iterFilters():
		is_mag = ( (sdss[filt]>=magRange[0]) & (sdss[filt]<=magRange[1]) )
		ref_ii = np.where(is_mag)[0]
		allTabs = []
		for utd in dataMap.iterUtDates():
			print 'calculating zero points for ',utd
			aperCatFn = '.'.join([pfx,utd,filt,'cat','fits'])
			files,frames = dataMap.getFiles('object',with_frames=True)
			if files is None:
				continue
			aperCat = fits.getdata(os.path.join(aperCatDir,aperCatFn))
			nAper = aperCat['counts'].shape[-1]
			aperCorrs = np.zeros((len(frames),nAper,4),dtype=np.float32)
			aperZps = np.zeros((len(frames),4),dtype=np.float32)
			psfZps = np.zeros_like(aperZps)
			for n,(f,i) in enumerate(zip(files,frames)):
				expTime =  dataMap.obsDb['expTime'][i]
				frameId =  dataMap.obsDb['frameIndex'][i]
				ii = np.where(aperCat['frameId']==frameId)[0]
				if len(ii)==0:
					print 'no data for frame ',f
					continue
				xCat = fits.open(dataMap('cat')(f))
				for ccd in range(1,5):
					# first for the aperture photometry
					c = np.where(aperCat['ccdNum'][ii]==ccd)[0]
					mask = ( (aperCat['counts'][ii[c],aperNum]<=0) |
					         (aperCat['flags'][ii[c],aperNum]>0) |
					         ~is_mag[aperCat['objId'][ii[c]]] )
					counts = np.ma.masked_array(
					            aperCat['counts'][ii[c],aperNum],mask=mask)
					aperMags = -2.5*np.ma.log10(counts/expTime)
					snr = counts / aperCat['countsErr'][ii[c],aperNum]
					refMags = sdss[filt][aperCat['objId'][ii[c]]]
					dMag = sigma_clip(refMags - aperMags)
					zp = np.ma.average(dMag,weights=snr**2)
					aperZps[n,ccd-1] = zp
					# then for the sextractor PSF mags
					m1,m2,s = srcor(xCat[ccd].data['ALPHA_J2000'],
					                xCat[ccd].data['DELTA_J2000'],
					                sdss['ra'][ref_ii],sdss['dec'][ref_ii],2.5)
					if len(m1)==0:
						print f,' has no catalog matches!!!'
						continue
					refMags = sdss[filt][ref_ii[m2]]
					psfMags = xCat[ccd].data['MAG_PSF'][m1]
					dMag = sigma_clip(refMags - psfMags)
					zp = np.ma.average(dMag)#,weights=snr**2)
					# have to convert from the sextractor zeropoint
					zp += 25.0 - 2.5*np.log10(expTime)
					psfZps[n,ccd-1] = zp
					# now aperture corrections
					mask = ( (aperCat['counts'][ii[c]]<=0) |
					         (aperCat['flags'][ii[c]]>0) |
					         ~is_mag[aperCat['objId'][ii[c]]][:,np.newaxis] )
					counts = np.ma.masked_array(
					            aperCat['counts'][ii[c]],mask=mask)
					refMags = sdss[filt][aperCat['objId'][ii[c]]]
					fratio = counts / counts[:,-1][:,np.newaxis]
					fratio = np.ma.masked_outside(fratio,0,1.5)
					fratio = sigma_clip(fratio,axis=0)
					aperCorrs[n,:,ccd-1] = (1/fratio).mean(axis=0).filled(0)
			aperCorrs = np.clip(aperCorrs,1,np.inf)
			tab = Table([np.repeat(utd,len(frames)),
			             dataMap.obsDb['frameIndex'][frames],
			             aperZps,psfZps,aperCorrs],
			            names=('utDate','frameId',
			                   'aperZp','psfZp','aperCorr'),
			            dtype=('S8','i4','f4','f4','f4'))
			allTabs.append(tab)
		tab = vstack(allTabs)
		tab.write('zeropoints_%s.fits'%filt,overwrite=True)

def match_to(ids1,ids2):
	idx = { j:i for i,j in enumerate(ids2) }
	return np.array([idx[i] for i in ids1])

def _read_old_catf(obsDb,catf):
	print 'loading ',catf
	dat1 = fits.getdata(catf,1)
	dat2 = fits.getdata(catf,2)
	idx = np.zeros(len(dat1),dtype=np.int32)
	for i,i1,i2 in zip(dat2['TINDEX'],dat2['i1'],dat2['i2']):
		idx[i1:i2] = i
	fns = [ f[:f.find('_ccd')] for f in dat1['fileName'] ]
	ii = match_to(fns,obsDb['fileName'])
	frameId = obsDb['frameIndex'][ii]
	t = Table([dat1['x'],dat1['y'],idx,
	           dat1['aperCounts'],dat1['aperCountsErr'],dat1['flags'],
	           dat1['ccdNum'],frameId],
	          names=('x','y','objId','counts','countsErr','flags',
	                 'ccdNum','frameId'))
	return t

def construct_lightcurves(dataMap,refCat,old=False):
	if old:
		pfx = refCat['filePrefix']
		# renaming
		pfx = {'sdss':'sdssbright'}.get(pfx,pfx)
		aperCatDir = os.environ['HOME']+'/data/projects/SDSS-RM/rmreduce/catalogs_v2b/'
		lcFn = lambda filt: 'lightcurves_%s_%s_old.fits' % (pfx,filt)
	else:
		pfx = refCat['filePrefix']
		aperCatDir = os.path.join(dataMap.procDir,'catalogs')
		lcFn = lambda filt: 'lightcurves_%s_%s.fits' % (pfx,filt)
	for filt in dataMap.iterFilters():
		allTabs = []
		for utd in dataMap.iterUtDates():
			if old and utd=='20131223':
				utd = '20131222'
			print 'loading catalogs from ',utd
			aperCatFn = '.'.join([pfx,utd,filt,'cat','fits'])
			aperCatF = os.path.join(aperCatDir,aperCatFn)
			if os.path.exists(aperCatF):
				if old:
					tab = _read_old_catf(dataMap.obsDb,aperCatF)
				else:
					tab = Table.read(aperCatF)
				allTabs.append(tab)
		tab = vstack(allTabs)
		print 'stacked aperture phot catalogs into table with ',
		print len(tab),' rows'
		tab.sort(['objId','frameId'])
		ii = match_to(tab['frameId'],dataMap.obsDb['frameIndex'])
		expTime = dataMap.obsDb['expTime'][ii][:,np.newaxis]
		try:
			apDat = Table.read('zeropoints_%s.fits'%filt)
			ii = match_to(tab['frameId'],apDat['frameId'])
			nAper = tab['counts'].shape[-1]
			apCorr = np.zeros((len(ii),nAper),dtype=np.float32)
			# cannot for the life of me figure out how to do this with indexing
			for apNum in range(nAper):
				apCorr[np.arange(len(ii)),apNum] = \
				            apDat['aperCorr'][ii,apNum,tab['ccdNum']-1]
			zp = apDat['aperZp'][ii]
			zp = zp[np.arange(len(ii)),tab['ccdNum']-1][:,np.newaxis]
			corrCps = tab['counts'] * apCorr / expTime
			magAB = zp - 2.5*np.ma.log10(np.ma.masked_array(corrCps,
			                                           mask=tab['counts']<=0))
			tab['aperMag'] = magAB.filled(99.99)
			tab['aperMagErr'] = 1.0856*tab['countsErr']/tab['counts']
			# convert AB mag to nanomaggie
			fluxConv = 10**(-0.4*(zp-22.5))
			tab['aperFlux'] = corrCps * fluxConv
			tab['aperFluxErr'] = (tab['countsErr']/expTime) * apCorr * fluxConv
		except IOError:
			pass
		ii = match_to(tab['frameId'],dataMap.obsDb['frameIndex'])
		tab['airmass'] = dataMap.obsDb['airmass'][ii]
		tab['mjd'] = dataMap.obsDb['mjd'][ii]
		tab.write(lcFn(filt),overwrite=True)

def nightly_lightcurves(catName,lcs=None,redo=False):
	from collections import defaultdict
	filt = 'g'
	if lcs is None:
		lcs = Table.read('lightcurves_%s_%s.fits'%(catName,filt))
	else:
		lcs = lcs.copy()
	lcFn = 'nightly_lcs_%s_%s.fits' % (catName,filt)
	if os.path.exists(lcFn) and not redo:
		print lcFn,' already exists, exiting'
		return
	# mjd-0.5 gives a nightly UT date, but pad with 0.1 because 
	# ~6am MT is >noon UT
	lcs['mjdInt'] = np.int32(np.floor(lcs['mjd']-0.6))
	# group by object and then by night
	lcs = lcs.group_by(['objId','mjdInt'])
	cols = defaultdict(list)
	for obj_night in lcs.groups:
		objId = obj_night['objId'][0]
		fluxes = np.ma.masked_array(obj_night['aperFlux'],
		                       mask=((obj_night['flags']>4) |
		                             ~np.isfinite(obj_night['aperFlux'])))
		# sigma_clip barfs even when nan's are masked...
		fluxes.data[np.isnan(fluxes.data)] = 0
		fluxes = sigma_clip(fluxes,iters=2,sigma=4.0,axis=0)
		errs = np.ma.masked_array(obj_night['aperFluxErr'],
		                          mask=~np.isfinite(obj_night['aperFluxErr']))
		# see above
		errs.data[np.isnan(errs.data)] = 0
		ivars = errs**-2
		flux,ivar = np.ma.average(fluxes,weights=ivars,axis=0,returned=True)
		mjd = obj_night['mjd'].mean()
		err = np.ma.sqrt(ivar)**-1
		df = fluxes - flux
		wsd = np.sqrt(np.sum(ivars*df**2,axis=0)/np.sum(ivars,axis=0))
		cols['objId'].append(objId)
		cols['aperFlux'].append(flux.filled(0))
		cols['aperFluxErr'].append(err.filled(0))
		cols['aperFluxWsd'].append(wsd.filled(0))
		cols['mean_mjd'].append(mjd)
		cols['nObs'].append(fluxes.shape[0])
		cols['nGood'].append((~fluxes[:,3].mask).sum())
	tab = Table(cols,names=('objId','mean_mjd',
	                        'aperFlux','aperFluxErr','aperFluxWsd',
	                        'nObs','nGood'))
	flux = np.ma.masked_array(tab['aperFlux'],mask=tab['aperFlux']<=0)
	mag = 22.5 - 2.5*np.ma.log10(flux)
	tab['aperMag'] = mag.filled(99.99)
	err = 1.0856*np.ma.divide(tab['aperFluxErr'],flux)
	tab['aperMagErr'] = err.filled(99.99)
	tab.write(lcFn,overwrite=redo)

def phot_stats(lcs,refPhot):
	from scipy.stats import scoreatpercentile
	band = 'g'
	apNum = 3
	if len(lcs.groups)==1:
		lcs = lcs.group_by('objId')
	medges = np.arange(16.9,19.11,0.2)
	mbins = medges[:-1] + np.diff(medges)/2
	all_dmag = []
	all_stds = []
	for mag1,mag2 in zip(medges[:-1],medges[1:]):
		ref_ii = np.where((refPhot[band]>mag1)&(refPhot[band]<mag2))[0]
		jj = np.where(np.in1d(lcs.groups.keys['objId'],ref_ii))[0]
		print 'found ',len(jj),' ref objs out of ',len(ref_ii)
		dmag = []
		stds = []
		for j in jj:
			mags = np.ma.masked_array(lcs.groups[j]['aperMag'][:,apNum],
			              mask=( (lcs.groups[j]['flags'][:,apNum]>0) |
			                     (lcs.groups[j]['aperMag'][:,apNum]>99) ) )
			if mags.mask.all():
				continue
			mags = sigma_clip(mags,iters=1,sigma=5.0)
			dmag.append((mags-mags.mean()).compressed())
			stds.append((mags-mags.mean()).std())
		dmag = np.concatenate(dmag)
		stds = np.array(stds)
		print mag1,mag2,dmag.std(),np.median(stds)
		all_dmag.append(dmag)
		all_stds.append([scoreatpercentile(stds,_p) for _p in [25,50,75]])
	all_stds = np.array(all_stds)
	return all_dmag,all_stds

def plot_compare_stds(stds,stds_old):
	import matplotlib.pyplot as plt
	medges = np.arange(16.9,19.11,0.2)
	mbins = medges[:-1] + np.diff(medges)/2
	def _append_arr(arr):
		return arr
		# used this for drawstyle=steps-post, but no equiv. for fill_between
		#return np.concatenate([arr,[arr[-1]]])
	plt.figure()
	for s,c in zip([stds_old,stds],'gb'):
		plt.fill_between(mbins,_append_arr(s[:,0]),_append_arr(s[:,2]),
		                 edgecolor='none',color=c,alpha=0.5)
		plt.plot(mbins,_append_arr(s[:,1]),color=c,lw=1.5)

def load_catalog(catName):
	dataDir = os.path.join(os.environ['SDSSRMDIR'],'data')
	if catName == 'sdssrm':
		cat = fits.getdata(os.path.join(dataDir,'target_fibermap.fits'),1)
		catPfx = 'bokrm'
	elif catName == 'sdss':
		cat = fits.getdata(os.path.join(dataDir,'sdss.fits'),1)
		catPfx = 'bokrm_sdss'
	elif catName == 'cfht':
		cat = fits.getdata(os.path.join(dataDir,'CFHTLSW3_starcat.fits'),1)
		catPfx = 'bokrm_cfht'
	return dict(catalog=cat,filePrefix=catPfx)

if __name__=='__main__':
	import argparse
	parser = argparse.ArgumentParser()
	parser = bokpl.init_file_args(parser)
	parser.add_argument('--catalog',type=str,default='sdssrm',
	                help='reference catalog ([sdssrm]|sdss|cfht)')
	parser.add_argument('--aperphot',action='store_true',
	                help='generate aperture photometry catalogs')
	parser.add_argument('--background',type=str,default='global',
	                help='background method to use for aperture phot ([global]|local|none)')
	parser.add_argument('--lightcurves',action='store_true',
	                help='construct lightcurves')
	parser.add_argument('--nightly',action='store_true',
	                help='construct nightly lightcurves')
	parser.add_argument('--zeropoint',action='store_true',
	                help='do zero point calculation')
	parser.add_argument('-p','--processes',type=int,default=1,
	                help='number of processes to use [default=single]')
	parser.add_argument('--old',action='store_true',
	                help='use 2014 catalogs for comparison')
	parser.add_argument('-v','--verbose',action='count',
	                    help='increase output verbosity')
	args = parser.parse_args()
	args = bokrmpipe.set_rm_defaults(args)
	dataMap = bokpl.init_data_map(args)
	dataMap = bokrmpipe.config_rm_data(dataMap,args)
	refCat = load_catalog(args.catalog)
	timerLog = bokutil.TimerLog()
	if args.aperphot:
		if args.processes == 1:
			procmap = map
		else:
			pool = multiprocessing.Pool(args.processes)
			procmap = pool.map
		aperture_phot(dataMap,refCat,procmap,redo=args.redo,
		              background=args.background)
		timerLog('aper phot')
		if args.processes > 1:
			pool.close()
	elif args.lightcurves:
		construct_lightcurves(dataMap,refCat,old=args.old)
		timerLog('lightcurves')
	elif args.nightly:
		nightly_lightcurves(refCat['filePrefix'],redo=args.redo)
		timerLog('night-avgd phot')
	elif args.zeropoint:
		zero_points(dataMap)
		timerLog('zeropoints')
	timerLog.dump()

