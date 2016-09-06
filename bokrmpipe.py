#!/usr/bin/env python

import os
import glob
import numpy as np
from astropy.table import Table

from bokpipe import bokpl,bokobsdb
from bokpipe import __version__ as pipeVersion

def set_rm_defaults(args):
	if args.rawdir is None:
		args.rawdir = os.environ['BOK90PRIMERAWDIR']
	if args.output is None:
		args.output = os.path.join(os.environ['BOK90PRIMEOUTDIR'],
		                           pipeVersion)
	if args.obsdb is None:
		args.obsdb = os.path.join('config','sdssrm-bok2014.fits.gz')
	return args

def make_obs_db(args):
	# all Bok observations during RM nights (incl. IBRM)
	fullObsDbFile = os.path.join('config','sdssrm-allbok.fits')
	if not os.path.exists(fullObsDbFile) or args.redo:
		utDirs = sorted(glob.glob(os.path.join(args.rawdir,'ut201?????')))
		print utDirs
		try:
			obsDb = Table.read(fullObsDbFile)
			print 'starting with existing ',fullObsDbFile
		except IOError:
			obsDb = None
		bokobsdb.generate_log(utDirs,fullObsDbFile,inTable=obsDb)
		obsDb = Table.read(fullObsDbFile)
		# fix problems
		# 1. files that are missing FILTER values
		missing = [ 'bokrm.20140314.%04d' % _i for _i in range(173,180) ]
		obsDb['filter'][np.in1d(obsDb['fileName'],missing)] = 'g'
		missing = [ 'bokrm.20140317.%04d' % _i for _i in [147,153] ]
		obsDb['filter'][np.in1d(obsDb['fileName'],missing)] = 'i'
		missing = [ 'bokrm.20140318.%04d' % _i for _i in [113] ]
		obsDb['filter'][np.in1d(obsDb['fileName'],missing)] = 'g'
		missing = [ 'bokrm.20140609.%04d' % _i for _i in [178] ]
		obsDb['filter'][np.in1d(obsDb['fileName'],missing)] = 'g'
		# 2. bad images
		good = np.ones(len(obsDb),dtype=bool)
		#       #1 flat has weird stripes
		bad = [ 'a%04d' % _i for _i in [1] ]
		good[(obsDb['utDate']=='20140114') &
		     np.in1d(obsDb['fileName'],bad)] = False
		#       #173 telescope was moving
		bad = [ 'bokrm.20140312.%04d' % _i for _i in [173] ]
		good[np.in1d(obsDb['fileName'],bad)] = False
		#       #26 flat is truncated
		bad = [ 'bokrm.20140319.%04d' % _i for _i in [26] ]
		good[np.in1d(obsDb['fileName'],bad)] = False
		#       #181 telescope was moving
		bad = [ 'bokrm.20140609.%04d' % _i for _i in [181] ]
		good[np.in1d(obsDb['fileName'],bad)] = False
		# write the edited table
		obsDb['good'] = good
		obsDb.write(fullObsDbFile,overwrite=True)
	obsDb = Table.read(fullObsDbFile)
	# all RM observations
	iszero = obsDb['imType']=='zero'
	isflat = ( (obsDb['imType']=='flat') & 
	           ((obsDb['filter']=='g')|(obsDb['filter']=='i')) )
	isrmfield = np.array([n.startswith('rm') for n in obsDb['objName']])
	isrmfield &= (obsDb['imType']=='object')
	isrm = iszero | isflat | isrmfield
	rmObsDbFile = os.path.join('config','sdssrm-bok.fits')
	obsDb[isrm].write(rmObsDbFile,overwrite=True)
	# all RM observations in 2014
	isrm2014 = isrm & (obsDb['mjd']<57000)
	rmObsDbFile = os.path.join('config','sdssrm-bok2014.fits')
	obsDb[isrm2014].write(rmObsDbFile,overwrite=True)

def load_darksky_frames(season,filt):
	t = Table.read(os.path.join('config',
	                            'bokrm%s_darksky_%s.txt'%(season,filt)),
	               format='ascii')
	ut = t['utDate']
	del t['utDate']
	t['utDate'] = ut.astype('S8')
	return t

if __name__=='__main__':
	import sys
	import argparse
	parser = argparse.ArgumentParser()
	parser = bokpl.init_file_args(parser)
	parser = bokpl.init_pipeline_args(parser)
	parser.add_argument('--darkskyframes',action='store_true',
	                help='load only the dark sky frames')
	parser.add_argument('--makeobsdb',action='store_true',
	                help='make the observations database')
	args = parser.parse_args()
	args = set_rm_defaults(args)
	if args.makeobsdb:
		# this needs to go here
		make_obs_db(args)
		sys.exit(0)
	dataMap = bokpl.init_data_map(args)
	dataMap = bokpl.set_master_cals(dataMap)
	if args.darkskyframes:
		# must select a band
		if args.band is None or args.band not in ['g','i']:
			raise ValueError("Must select a band for dark sky frames (-b)")
		frames = load_darksky_frames('2014',args.band) # XXX
		dataMap.setFileList(frames['utDate'],frames['fileName'])
	bokpl.run_pipe(dataMap,args)

