#!/usr/bin/python2

import os, sys
from datetime import datetime, date, timedelta
from subprocess import Popen, PIPE, check_call
from backupcommon import BackupLock, BackupLogger, info, debug, error, exception, OracleExec, Configuration, BackupTemplate, create_snapshot_class
from ConfigParser import SafeConfigParser
from tempfile import mkstemp, TemporaryFile
from random import randint

def printhelp():
  print "Usage: autorestore.py <configuration_file_name without directory> [config]"
  print "  [config] is optional, if missed then action is performed on all databases in config file."
  print "  [config] could either be database unique name to be restore or on of the repository actions:"
  print "    --createcatalog"
  print "    --listvalidationdates"
  sys.exit(2)

if len(sys.argv) not in [2,3]:
  printhelp()

if os.geteuid() == 0:
  print "No, I will not run as root."
  sys.exit(0)

if (not os.getenv('AUTORESTORE_SAFE_SANDBOX')) or (os.environ['AUTORESTORE_SAFE_SANDBOX'] != 'TRUE'):
  print "THIS AUTORESTORE PROCESS CAN BE VERY DANGEROUS IF THIS HOST HAS ACCESS TO PRODUCTION DATABASE FILESYSTEM/STORAGE."
  print "THE RESTORE PROCESS CAN OVERWRITE OR DELETE FILES ON THEIR ORIGINAL CONTROL FILE LOCATIONS!"
  print "RUN IT ONLY ON A HOST THAT IS COMPLETELY SANDBOXED FROM PRODUCTION DATABASE ENVIRONMENT."
  print "TO CONTINUE, SET ENVIRONMENT VARIABLE AUTORESTORE_SAFE_SANDBOX TO VALUE TRUE (CASE SENSITIVE)."
  print ""
  sys.exit(3)

Configuration.init('autorestore', configfilename=sys.argv[1], additionaldefaults={'customverifydate': 'select max(time_dp) from sys.smon_scn_time','autorestoreenabled': '1',
  'autorestoreinstancenumber': '1', 'autorestorethread': '1'})
OracleExec.init(Configuration.get('oraclehome', 'generic'))
maxtolerance = timedelta(minutes=int(Configuration.get('autorestoremaxtoleranceminutes','autorestore')))
validatechance = int(Configuration.get('autorestorevalidatechance', 'autorestore'))
validatemodulus = int(Configuration.get('autorestoremodulus', 'autorestore'))

# Does the backup destination exist?
restoredest = Configuration.get('autorestoredestination','autorestore')
mountdest = Configuration.get('autorestoremountpoint','autorestore')
logdir = Configuration.get('autorestorelogdir','autorestore')
Configuration.substitutions.update({
  'autorestoredestination': restoredest,
  'mountdestination': mountdest,
  'logdir': logdir,
  'autorestorecatalog': Configuration.get('autorestorecatalog','autorestore')
})
if restoredest is None or not os.path.exists(restoredest) or not os.path.isdir(restoredest):
  print "Restore directory %s not found or is not a proper directory" % restoredest
  sys.exit(2)
if mountdest is None or not os.path.exists(mountdest) or not os.path.isdir(mountdest):
  print "Clone mount directory %s not found or is not a proper directory" % mountdest
  sys.exit(2)

# Read autorestore template
restoretemplate = BackupTemplate('restoretemplate.cfg')
initfile = restoretemplate.get('initoralocation')
Configuration.substitutions.update({'initora': initfile})

exitstatus = 0

# System actions

# Clean destination directory
def cleantarget():
  debug("ACTION: Cleaning destination directory %s" % restoredest)
  for root, dirs, files in os.walk(restoredest, topdown=False):
    for name in files:
      os.remove(os.path.join(root, name))
    for name in dirs:
      os.rmdir(os.path.join(root, name))

def validationdate(database):
  days_since_epoch = (datetime.utcnow() - datetime(1970,1,1)).days
  try:
    hashstring = Configuration.get('stringforvalidationmod', database)
  except:
    hashstring = database
  mod1 = days_since_epoch % validatemodulus
  mod2 = hash(hashstring) % validatemodulus
  validatecorruption = mod1 == mod2
  days_to_next_validation = (mod2-mod1) if mod2 > mod1 else (validatemodulus-(mod1-mod2))
  next_validation = date.today() + timedelta(days=days_to_next_validation)
  return (validatecorruption, days_to_next_validation, next_validation)


# Oracle actions

def createinitora(filename):
  with open(filename, 'w') as f:
    contents = restoretemplate.get('autoinitora')
    if 'cdb' in Configuration.substitutions and Configuration.substitutions['cdb'].upper() == 'TRUE':
      contents+= restoretemplate.get('cdbinitora')
    debug("ACTION: Generated init file %s\n%s" % (filename, contents))
    f.write(contents)

def exec_sqlplus(commands, headers=True, returnoutput=False):
  if headers:
    finalscript = "%s\n%s\n%s" % (restoretemplate.get('sqlplusheader'), commands, restoretemplate.get('sqlplusfooter'))
  else:
    finalscript = commands
  return OracleExec.sqlplus(finalscript, silent=returnoutput)

def exec_rman(commands):
  finalscript = "%s\n%s\n%s" % (restoretemplate.get('rmanheader'), commands, restoretemplate.get('rmanfooter'))
  OracleExec.rman(finalscript)

# Orchestrator

def runrestore(database):
  global exitstatus
  #
  Configuration.defaultsection = database
  # Reinitialize Oracle execution environment since new database may have different home
  OracleExec.init(Configuration.get('oraclehome', 'generic'))
  # Reinitialize logging
  BackupLogger.init(os.path.join(logdir, "%s-%s.log" % (datetime.now().strftime('%Y%m%dT%H%M%S'), database)), database)
  BackupLogger.clean()
  #
  starttime = datetime.now()
  info("Starting to restore")
  info("Logfile: %s" % BackupLogger.logfile)
  Configuration.substitutions.update({
      'logfile': BackupLogger.logfile
  })
  snap = create_snapshot_class(database)
  cleantarget()
  sourcesnapid = 'unknown'
  try:
    sourcesnapid = snap.autoclone()
  except:
    exitstatus = 1
    exception("Cloning failed, but we continue with the next database.")
    return

  try:
    check_call(['mount', mountdest])
  except:
    exitstatus = 1
    exception("Mounting failed, but we continue with the next database.")
    return

  success = False
  verifyseconds = -1
  #
  if validatemodulus > 0:
    # Validation based on modulus
    validationinfo = validationdate(database)
    validatecorruption = validationinfo[0]
    if not validatecorruption:
      debug("Next database validation in %d days: %s" % ( validationinfo[1], validationinfo[2] ))
  else:
    # Validation based on random
    validatecorruption = (validatechance > 0) and (randint(1, validatechance) == validatechance)
  if validatecorruption:
    debug("Database will be validated during this restore session")
  #
  try:
    dbconfig = SafeConfigParser()
    dbconfig.read(os.path.join(mountdest, 'autorestore.cfg'))
    dbname = dbconfig.get('dbparams','db_name')
    customverifydate = Configuration.get('customverifydate', database)
    bctfile = dbconfig.get('dbparams','bctfile')
    Configuration.substitutions.update({
      'db_name': dbname,
      'db_compatible': dbconfig.get('dbparams','compatible'),
      'db_files': dbconfig.get('dbparams','db_files'),
      'db_undotbs': dbconfig.get('dbparams','undo_tablespace'),
      'db_block_size': dbconfig.get('dbparams','db_block_size'),
      'lastscn': dbconfig.get('dbparams','lastscn'),
      'lasttime': dbconfig.get('dbparams','lasttime'),
      'dbid': Configuration.get('dbid', database),
      'instancenumber': Configuration.get('autorestoreinstancenumber', database),
      'thread': Configuration.get('autorestorethread', database),
      'backupfinishedtime': dbconfig.get('dbparams','backup-finished'),
      'customverifydate': customverifydate,
      'bctfile': bctfile
    })
    try:
      Configuration.substitutions.update({'cdb': dbconfig.get('dbparams','enable_pluggable_database')})
    except:
      Configuration.substitutions.update({'cdb': 'FALSE'})
    #
    createinitora(initfile)
    os.environ["ORACLE_SID"] = dbname
    debug('ACTION: startup nomount')
    exec_sqlplus(restoretemplate.get('startupnomount'))
    debug('ACTION: mount database and catalog files')
    exec_rman(restoretemplate.get('mountandcatalog'))
    if bctfile:
      debug('ACTION: disable block change tracking')
      exec_sqlplus(restoretemplate.get('disablebct'))
    debug('ACTION: create missing datafiles')
    output = exec_sqlplus(restoretemplate.get('switchdatafiles'), returnoutput=True)
    switchdfscript = ""
    for line in output.splitlines():
      if line.startswith('RENAMEDF-'):
        switchdfscript+= "%s\n" % line.strip()[9:]
    debug('ACTION: switch and recover')
    exec_rman("%s\n%s" % (switchdfscript, restoretemplate.get('recoverdatafiles')))
    debug('ACTION: opening database to verify the result')
    #
    verifytime = None
    output = exec_sqlplus(restoretemplate.get('openandverify'), returnoutput=True)
    for line in output.splitlines():
      if line.startswith('CUSTOM VERIFICATION TIME:'):
        verifytime = datetime.strptime(line.split(':', 1)[1].strip(), '%Y-%m-%d %H:%M:%S')
    if verifytime is None:
      raise Exception('autorestore', 'Reading verification time failed.')
    lastrestoretime = datetime.strptime(dbconfig.get('dbparams','lasttime'), '%Y-%m-%d %H:%M:%S')
    verifydiff = lastrestoretime-verifytime
    verifyseconds = int(verifydiff.seconds + verifydiff.days * 24 * 3600)
    debug("Expected time: %s" % lastrestoretime)
    debug("Verified time: %s" % verifytime)
    debug("VERIFY: Time difference %s" % verifydiff)
    #
    if validatecorruption:
      info("ACTION: Validating database for corruptions")
      # The following command will introduce some corruption to test database validation
      # check_call(['dd','if=/dev/urandom','of=/nfs/autorestore/mnt/data_D-ORCL_I-1373437895_TS-SOE_FNO-5_0sqov4pv','bs=8192','count=10','seek=200','conv=notrunc' ])
      try:
        exec_rman(restoretemplate.get('validateblocks'))
      finally:
        exec_sqlplus(restoretemplate.get('showcorruptblocks'))
    #
    debug('ACTION: shutdown')
    exec_sqlplus(restoretemplate.get('shutdown'))
    #
    if verifydiff > maxtolerance:
      raise Exception('autorestore', "Verification time difference %s is larger than allowed tolerance %s" % (verifydiff, maxtolerance))
    #
    success = True
  except:
    # When error happens here, we can try another database
    exception("Error happened, but we can continue with the next database.")
    try:
      debug('ACTION: In case instance is still running, aborting it')
      exec_sqlplus(restoretemplate.get('shutdownabort'))
    except:
      pass
  # Finish up
  check_call(['umount', mountdest])
  snap.dropautoclone()
  endtime = datetime.now()
  if not success:
    exitstatus = 1
  Configuration.substitutions.update({
    'log_dbname': database,
    'log_start': starttime.strftime('%Y-%m-%d %H-%M-%S'),
    'log_stop': endtime.strftime('%Y-%m-%d %H-%M-%S'),
    'log_success': '1' if success else '0',
    'log_diff': verifyseconds,
    'log_snapid': sourcesnapid,
    'log_validated': '1' if validatecorruption else '0'
  })
  debug('Logging the result to catalog.')
  try:
    OracleExec.sqlldr(Configuration.get('autorestorecatalog','autorestore'), restoretemplate.get('sqlldrlog'))
  except:
    pass
  try:
    exec_sqlplus(restoretemplate.get('insertlog'), False, returnoutput=True)
  except:
    exception("Logging the result to catalog failed.")
  info("Restore %s, elapsed time: %s" % ('successful' if success else 'failed', endtime-starttime))
  BackupLogger.close(True)

# UI

def loopdatabases():
  excludelist = ['generic','rman','zfssa','autorestore']
  for configname in Configuration.sections():
    if configname not in excludelist:
      if Configuration.get('autorestoreenabled', configname) == '1':
        yield configname

action = None
if len(sys.argv) == 3:
  action = sys.argv[2]

if action == '--listvalidationdates':
  # This action does not need a lock
  if validatemodulus > 0:
    for configname in loopdatabases():
      validationinfo = validationdate(configname)
      print "%s: %s (in %d days)" % (configname, validationinfo[2], validationinfo[1])
  else:
    if validatechance > 0:
      print "Validation is based on chance, probability 1/%d" % validatechance
    else:
      print "Database validation is not turned on"
else:
  # Actions that need a lock
  lock = BackupLock(Configuration.get('autorestorelogdir','autorestore'))
  try:
    if action is not None:
      if action.startswith('--'):
        if action == '--createcatalog':
          BackupLogger.init(os.path.join(logdir, "%s-config.log" % (datetime.now().strftime('%Y%m%dT%H%M%S'))), 'config')
          info("Logfile: %s" % BackupLogger.logfile)
          Configuration.substitutions.update({'logfile': BackupLogger.logfile})
          exec_sqlplus(restoretemplate.get('createcatalog'), False)
      else:
        runrestore(action)
    else:
      # Loop through all sections
      for configname in loopdatabases():
        runrestore(configname)
      # Run ADRCI to clean up diag
      adrage = int(Configuration.get('logretention','generic'))*1440
      f1 = mkstemp(suffix=".adi")
      ftmp = os.fdopen(f1[0], "w")
      ftmp.write("set base %s\n" % logdir)
      ftmp.write("show homes\n")
      ftmp.close()
      f2 = mkstemp(suffix=".adi")
      ftmp2 = os.fdopen(f2[0], "w")
      ftmp2.write("set base %s\n" % logdir)
      with TemporaryFile() as f:
        p = Popen([os.path.join(OracleExec.oraclehome, 'bin', 'adrci'), "script=%s" % f1[1]], stdout=f, stderr=None, stdin=None)
        p.wait()
        if p.returncode == 0:
          f.seek(0,0)
          output = f.read()
          startreading = False
          for line in output.splitlines():
            if line.startswith('ADR Homes:'):
              startreading = True
            elif startreading:
              ftmp2.write("set home %s\n" % line.strip())
              ftmp2.write("purge -age %d\n" % adrage)
        else:
          print "Executing ADRCI failed."
      ftmp2.close()
      os.unlink(f1[1])
      with TemporaryFile() as f:
        p = Popen([os.path.join(OracleExec.oraclehome, 'bin', 'adrci'), "script=%s" % f2[1]], stdout=f, stderr=None, stdin=None)
        p.wait()
        if p.returncode != 0:
          print "Executing ADRCI failed."
      os.unlink(f2[1])
  finally:
    lock.release()
    print "Exitstatus is %d" % exitstatus
    sys.exit(exitstatus)