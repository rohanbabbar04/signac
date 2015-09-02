import datetime
import logging
import os
import re
import shutil
import tarfile
import warnings
import collections
import copy
import itertools
from queue import Queue
from tempfile import TemporaryDirectory
from logging.handlers import QueueHandler, QueueListener
from datetime import datetime, timedelta

import pymongo
import pymongo.errors
from bson.json_util import dumps
from bson.json_util import loads

from .. import VERSION, VERSION_TUPLE
from ..core.config import load_config
from ..core.dbclient_connector import DBClientConnector
from ..core.mongodb_executor import MongoDBExecutor
from ..core.mongodb_queue import Empty
from ..core.mongodb_queue import MongoDBQueue
from ..core.mongodb_set import MongoDBSet
from ..core.serialization import encode_callable_filter
from .cache import Cache
from .concurrency import DocumentLock
from .errors import ConnectionFailure, DatabaseError
from .job_pool import JobPool
from .logging import MongoDBHandler, record_from_doc
from .milestones import Milestones
from .snapshot import dump_db, restore_db
from .templates import RESTORE_SH
from .job import OnlineJob, OfflineJob, PULSE_PERIOD
from .hashing import generate_hash_from_spec
from .constants import *

logger = logging.getLogger(__name__)

PYMONGO_3 = pymongo.version_tuple[0] == 3

def valid_name(name):
    return not name.startswith(SIGNAC_PREFIX)

class BaseProject(object):
    """Base class for all project classes.

    All properties and methods in this class do not require a online database connection.
    Application developers should usually not need to instantiate this class.
    See ``Project`` instead.
    """
    
    def __init__(self, config = None):
        """Initializes a BaseProject instance.

        Application developers should usually not need to instantiate this class.
        See ``Project`` instead.
        """
        if config is None:
            config = load_config()
        config.verify()
        self._config = config
        self._job_queue = None
        self._job_queue_ = None
        self._fetched_set = None

    def __str__(self):
        "Returns the project's id."
        return str(self.get_id())

    @property 
    def config(self):
        "The project's configuration."
        return self._config

    def root_directory(self):
        "Returns the project's root directory as determined from the configuration."
        return self._config['project_dir']

    def get_id(self):
        """"Returns the project's id as determined from the configuration.

        :returns: str - The project id.
        :raises: KeyError

        This method raises ``KeyError`` if no project id could be determined.
        """
        try:
            return str(self.config['project'])
        except KeyError:
            msg = "Unable to determine project id. "
            msg += "Are you sure '{}' is a compDB project path?"
            raise LookupError(msg.format(os.path.abspath(os.getcwd())))

    def open_offline_job(self, parameters = None):
        """Open an offline job, specified by its parameters.

        :param parameters: A dictionary specifying the job parameters.
        :returns: An instance of OfflineJob.
        """
        return OfflineJob(
            project = self,
            parameters = parameters)

    def filestorage_dir(self):
        "Return the project's filestorage directory."
        return self.config['filestorage_dir']

    def workspace_dir(self):
        "Return the project's workspace directory."
        return self.config['workspace_dir']

    def _filestorage_dir(self):
        warnings.warn("The method '_filestorage_dir' is deprecated.", DeprecationWarning)
        return self.config['filestorage_dir']

    def _workspace_dir(self):
        warnings.warn("The method '_workspace_dir' is deprecated.", DeprecationWarning)
        return self.config['workspace_dir']

    def get_milestones(self, job_id):
        warnings.warn("The milestone API will be deprecated in the future.", PendingDeprecationWarning)
        return Milestones(self, job_id)

    def get_cache(self):
        warnings.warn("The cache API may be deprecated in the future.", PendingDeprecationWarning)
        return Cache(self)

    def develop_mode(self):
        warnings.warn("The develop_mode API may be deprecated in the future.", PendingDeprecationWarning)
        return bool(self.config.get('develop', False))

    def activate_develop_mode(self):
        warnings.warn("The develop_mode API may be deprecated in the future.", PendingDeprecationWarning)
        msg = "{}: Activating develop mode!"
        logger.warning(msg.format(self.get_id()))
        self.config['develop'] = True

class RollBackupExistsError(RuntimeError):
    pass

class OnlineProject(BaseProject):
    """OnlineProject extends BaseProject with properties and methods that require a database connection."""

    def __init__(self, config = None, client = None):
        """Initialize a Onlineproject.

        :param config:     A signac configuration instance.
        :param client:     A pymongo client instance.
            
        Both arguments are optional.
        If no config is provided, it will be fetched from the environment.
        If no client is provided, the client will be instantiated from the configuration when needed.

        .. note::
           Some methods in this class requires an online connection to a database!
        """
        super(OnlineProject, self).__init__(config=config)
        self._client = client
        # Online logging
        self._loggers = [logging.getLogger('signac')]
        self._logging_queue = Queue()
        self._logging_queue_handler = QueueHandler(self._logging_queue)
        self._logging_listener = None

    def _get_client(self):
        "Attempt to connect to the database host and store the client instance."
        if self._client is None:
            prefix = 'database_'
            connector = DBClientConnector(self.config, prefix = prefix)
            logger.debug("Connecting to database.")
            try:
                connector.connect()
                connector.authenticate()
            except:
                logger.error("Connection failed.")
                raise
            else:
                logger.debug("Connected and authenticated.")
            self._client = connector.client
        return self._client

    def _get_db(self, db_name):
        "Return a database with name :param db_name: from the database host."
        host = self.config['database_host']
        try:
            return self._get_client()[db_name]
        except pymongo.errors.ConnectionFailure as error:
            msg = "Failed to connect to database '{}' at '{}'."
            #logger.error(msg.format(db_name, host))
            raise ConnectionFailure(msg.format(db_name, host)) from error

    def get_db(self, db_name = None):
        """Return a database from the project's database host.

        :param db_name: The name of the database.
        :returns: The datbase with :param db_name: or the project's root database.
        """
        if db_name is None:
            return self.get_db(self.get_id())
        else:
            assert valid_name(db_name)
            return self._get_db(db_name)

    def get_project_db(self):
        "Return the project's root database."
        warnings.warn("The method 'get_project_db' will be deprecated in the future. Use 'get_db' instead.", PendingDeprecationWarning)
        return self.get_db(self.get_id())

    def _get_meta_db(self):
        return self.get_db()

    def get_jobs_collection(self):
        warnings.warn("The method 'get_jobs_collection' is no longer part of the public API.", DeprecationWarning)
        return self._get_jobs_collection()

    def _get_jobs_collection(self):
        return self._get_meta_db()[JOB_META_DOCS]

    @property
    def _collection(self):
        return self.get_db()[JOB_DOCS]

    @property
    def collection(self):
        warnings.warn("The property 'collection' is no longer part of the public API.", DeprecationWarning)
        return self._collection

    def _parameters_from_id(self, job_id):
        "Determine a job's parameters from the job id."
        result = self._get_jobs_collection().find_one(job_id, [JOB_PARAMETERS_KEY])
        if result is None:
            raise KeyError(job_id)
        try:
            return result[JOB_PARAMETERS_KEY]
        except KeyError as error:
            msg = "Unable to retrieve parameters for job '{}'. Database corrupted."
            raise DatabaseError(msg.format(job_id))

    def register_job(self, parameters = None):
        """Register a job for this project.

        :param parameters: A dictionary specifying the job parameters.
        """
        job = OnlineJob(self, parameters=parameters)
        job._register_online()

    def open_job(self, parameters = None, blocking = True, timeout = -1):
        """Open an online job, specified by its parameters.

        :param parameters: A dictionary specifying the job parameters.
        :param blocking: Block until the job is openend.
        :param timeout: Wait a maximum of :param timeout: seconds. A value -1 specifies to wait infinitely.
        :returns: An instance of OnlineJob.
        :raises: DocumentLockError

        .. note::
           This method will raise a DocumentLockError if it was impossible to open the job within the specified timeout.
        """
        return OnlineJob(
            project = self,
            parameters = parameters,
            blocking = blocking,
            timeout = timeout)

    def _open_job(self, **kwargs):
        return OnlineJob(project=self, **kwargs)

    def open_job_from_id(self, job_id, blocking = True, timeout = -1):
        """Open an online job, specified by its job id.

        :param job_id: The job's job_id.
        :param blocking: Block until the job is openend.
        :param timeout: Wait a maximum of :param timeout: seconds. A value -1 specifies to wait infinitely.
        :returns: An instance of OnlineJob.
        :raises: DocumentLockError

        .. warning: The job must be registered in the database, otherwise it is impossible to determine the parameters.

        .. note::
           This method will raise a DocumentLockError if it was impossible to open the job within the specified timeout.
        """
        return OnlineJob(
            project = self,
            parameters = self._parameters_from_id(job_id),
            blocking = blocking, timeout = timeout)

    def find_jobs(self, job_spec = None, spec = None, blocking = True, timeout = -1):
        """Find jobs, specified by the job's parameters and/or the job's document.

        :param job_spec: The filter for the job parameters.
        :param spec: The filter for the job document.
        :returns: An iterator over all OnlineJob instances, that match the criteria.
        """
        if job_spec is None:
            job_spec = {JOB_PARAMETERS_KEY: {'$exists': True}}
        else:
            job_spec = {JOB_PARAMETERS_KEY+'.{}'.format(k): v for k,v in job_spec.items()}
        job_ids = list(self._find_job_ids(job_spec))
        if spec is not None:
            spec.update({'_id': {'$in': job_ids}})
            docs = self._collection.find(spec)
            job_ids = (doc['_id'] for doc in docs)
        for _id in job_ids:
            yield self.open_job_from_id(_id, blocking, timeout)

    def _active_job_ids(self):
        "Returns an iterator over all job_ids of active online jobs."
        spec = {
            'executing': {'$exists': True},
            '$where': 'this.executing.length > 0'}
        yield from self._find_job_ids(spec)

    def active_jobs(self, blocking = True, timeout = -1):
        "Returns an iterator over all active jobs."
        warnings.warn("This method returns actual jobs, not job ids now!")
        for _id in self._active_job_ids():
            yield self.open_job_from_id(_id, blocking, timeout)

    def num_active_jobs(self):
        spec = {
            'executing': {'$exists': True},
            '$where': 'this.executing.length > 0'}
        return self._get_jobs_collection().find(spec).count()

    def find(self, job_spec = {}, spec = {}, * args, ** kwargs):
        """Find job documents, specified by the job's parameters and/or the job's document.

        :param job_spec: The filter for the job parameters.
        :param spec: The filter for the job document.
        :returns: Documents matching all specifications.
        """
        job_ids = self._find_job_ids(job_spec)
        spec.update({'_id': {'$in': list(job_ids)}})
        yield from self._collection.find(spec, * args, ** kwargs)

    def clear(self, force = False):
        """Clear the project jobs and logs.
        
        .. note::
           Clearing the project is permanent. Use with caution!"""
        self.clear_logs()
        for job in self.find_jobs():
            job.remove(force = force)

    def remove(self, force = False):
        """Remove all jobs, logs and the complete project database.
        
          .. note::
             The removal is permanent. Use with caution!"""
        try:
            self.clear(force = force)
        except Exception as error:
            if force:
                logger.error("Error during clearing. Forced to ignore.")
            else:
                raise
        try:
            host = self.config['database_host']
            client = self._get_client()
            client.drop_database(self.get_id())
        except pymongo.errors.ConnectionFailure as error:
            msg = "{}: Failed to remove project database on '{}'."
            raise ConnectionFailure(msg.format(self.get_id(), host)) from error

    def _lock_job(self, job_id, blocking = True, timeout = -1):
        "Lock the job document of job with ``job_id``."
        return DocumentLock(
            self._get_jobs_collection(), job_id,
            blocking = blocking, timeout = timeout)

    def lock_job(self, *args, **kwargs):
        warnings.warn("The method 'lock_job' will be no longer part of the public API in the future.", PendingDeprecationWarning)
        return self._lock_job(*args, **kwargs)

    def _unique_jobs_from_pulse(self):
        docs = self._get_jobs_collection().find(
            {'pulse': {'$exists': True}},
            ['pulse'])
        beats = [doc['pulse'] for doc in docs]
        for beat in beats:
            for uid, timestamp in beat.items():
                yield uid

    def job_pulse(self):
        uids = self._unique_jobs_from_pulse()
        for uid in uids:
            hb_key = 'pulse.{}'.format(uid)
            doc = self._get_jobs_collection().find_one(
                {hb_key: {'$exists': True}})
            yield uid, doc['pulse'][uid]

    def clear_develop(self, force = True):
        spec = {'develop': True}
        job_ids = self._find_job_ids(spec)
        for develop_job in self.find_jobs(spec):
            develop_job.remove(force = force)
        self._collection.remove({'id': {'$in': list(job_ids)}})

    def kill_dead_jobs(self, seconds = 5 * PULSE_PERIOD):
        cut_off = datetime.utcnow() - timedelta(seconds = seconds)
        uids = self._unique_jobs_from_pulse()
        for uid in uids:
            hbkey = 'pulse.{}'.format(uid)
            doc = self._get_jobs_collection().update(
                #{'pulse.{}'.format(uid): {'$exists': True},
                {hbkey: {'$lt': cut_off}},
                {   '$pull': {'executing': uid},
                    '$unset': {hbkey: ''},
                })

    def _get_links(self, url, parameters, fs):
        for w in self._walk_job_docs(parameters):
            src = os.path.join(fs, w['_id'])
            try:
                dd = collections.defaultdict(lambda: 'None')
                dd.update(w['parameters'])
                dst = url.format(** dd)
            except KeyError as error:
                msg = "Unknown parameter: {}"
                raise KeyError(msg.format(error)) from error
            yield src, dst

    def get_storage_links(self, url = None):
        if url is None:
            url = self.get_default_view_url()
        parameters = re.findall('\{\w+\}', url)
        yield from self._get_links(
            url, parameters, self.filestorage_dir())

    def get_workspace_links(self, url = None):
        if url is None:
            url = self.get_default_view_url()
        parameters = re.findall('\{\w+\}', url)
        yield from self._get_links(
            url, parameters, self.workspace_dir())

    def _aggregate_parameters(self, job_spec = None, uniqueonly=False):
        pipe = [
            {'$match': job_spec or dict()},
            {'$group': {
                '_id': False,
                'parameters': { '$addToSet': '$parameters'}}},
            ]
        result = self._get_jobs_collection().aggregate(pipe)
        parameters = collections.defaultdict(set)
        if PYMONGO_3:
            for doc in result:
                for p in doc['parameters']:
                    for k, v in p.items():
                        try:
                            parameters[k].add(v)
                        except TypeError:
                            parameters[k].add(generate_hash_from_spec(v))
        else:
            assert result['ok']
            if len(result['result']):
                for p in result['result'][0]['parameters']:
                    for k, v in p.items():
                        parameters[k].add(v)
        return set(k for k,v in sorted(parameters.items(), key=lambda i:len(i[1])) if not uniqueonly or len(v) > 1)

    def get_default_view_url(self):
        params = sorted(self._aggregate_parameters(uniqueonly=True))
        if len(params):
            return str(os.path.join(* itertools.chain.from_iterable(
                (str(p), '{'+str(p)+'}') for p in params)))
        else:
            return str()

    def create_view(self, url = None, make_copy = False, workspace = False, prefix = None):
        if url is None:
            url = self.get_default_view_url()
        if prefix is not None:
            url = os.path.join(prefix, url)
        parameters = re.findall('\{\w+\}', url)
        if workspace:
            links = self._get_links(url, parameters, self.workspace_dir())
        else:
            links = self._get_links(url, parameters, self.filestorage_dir())
            for src, dst in links:
                self._make_link(src, dst, make_copy=make_copy)

    def _make_link(self, src, dst, make_copy=False):
        try:
            os.makedirs(os.path.dirname(dst))
        except FileExistsError:
            pass
        try:
            if make_copy:
                shutil.copytree(src, dst)
            else:
                os.symlink(src, dst, target_is_directory = True)
        except FileExistsError as error:
            msg = "Failed to create view for url '{url}'. "
            msg += "Possible causes: A view with the same path exists already or you are not using enough parameters for uniqueness."
            raise RuntimeError(msg)

    def create_view_script(self, url=None, cmd=None, workspace=False, prefix=None, fs=None):
        if cmd is None:
            cmd = 'mkdir -p {head}\nln -s {src} {head}/{tail}'
        if url is None:
            url = self.get_default_view_url()
        if prefix is not None:
            url = os.path.join(prefix, url)
        parameters = re.findall('\{\w+\}', url)
        if fs is None:
            fs = self.workspace_dir() if workspace else self.filestorage_dir()
        for src, dst in self._get_links(url, parameters, fs):
            head, tail = os.path.split(dst)
            yield cmd.format(src = src, head = head, tail = tail)

    def create_flat_view(self, job_spec=None, prefix=None):
        if prefix is None:
            prefix = os.getcwd()
        for fs_dst, fs_src in [ ('workspace', self.workspace_dir()),
                                ('storage', self.filestorage_dir())]:
            for job_id in self._find_job_ids():
                src = os.path.join(fs_src, job_id)
                dst = os.path.join(prefix, fs_dst, job_id)
                self._make_link(src, dst)

    def _create_db_snapshot(self, dst):
        job_docs = self._get_jobs_collection().find()
        docs = self._collection.find()
        fn_dump_jobs = os.path.join(dst, FN_DUMP_JOBS)
        with open(fn_dump_jobs, 'wb') as file:
            for job_doc in job_docs:
                file.write("{}\n".format(dumps(job_doc)).encode())
        fn_dump_db = os.path.join(dst, FN_DUMP_DB)
        dump_db(self.get_db(), fn_dump_db)
        return [fn_dump_jobs, fn_dump_db]

    def _create_config_snapshot(self, dst):
        fn_config = os.path.join(dst, 'signac.rc')
        self.config.write(fn_config)
        return [fn_config]

    def _create_restore_scripts(self, dst):
        fn_restore_script_sh = os.path.join(dst, FN_RESTORE_SCRIPT_SH)
        with open(fn_restore_script_sh, 'wb') as file:
            file.write("#/usr/bin/env sh\n# -*- coding: utf-8 -*-\n".encode())
            file.write(RESTORE_SH.format(
                project = self.get_id(),
                db_host = self.config['database_host'],
                fs_dir = self.filestorage_dir(),
                db_meta = self.config['database_meta'],
                signac_docs = JOB_META_DOCS,
                signac_job_docs = JOB_DOCS,
                sn_storage_dir= FN_DUMP_STORAGE,
                    ).encode())
        return [fn_restore_script_sh]

    def _create_config_snapshot(self, dst):
        fn_config = os.path.join(dst, 'signac.rc')
        self.config.write(fn_config)
        return [fn_config]

    def _create_snapshot_view_script(self, dst):
        fn_script = os.path.join(dst, 'link_view.sh')
        with open(fn_script, 'wb') as file:
            file.write("CMD_LINK='ln -s'\n".encode())
            file.write("CMD_MKDIR='mkdir -p'\n".encode())
            cmd = "$CMD_MKDIR view/{head}\n$CMD_LINK {src} view/{head}/{tail}\n"
            for line in self.create_view_script(
                cmd = cmd, fs = FN_DUMP_STORAGE):
                file.write(line.encode())
        return [fn_script]

    def _check_snapshot(self, src):
        # TODO Implement check of content routine!
        if os.path.isfile(src):
            with tarfile.open(src, 'r') as file:
                pass
        elif os.path.isdir(src):
            pass
        else:
            raise FileNotFoundError(src)

    def _restore_snapshot_from_src(self, src, force = False):
        fn_storage = os.path.join(src, FN_DUMP_STORAGE)
        try:
            with open(os.path.join(src, FN_DUMP_JOBS), 'rb') as file:
                for job in self.find_jobs():
                    job.remove()
                for line in file:
                    job_doc = loads(line.decode())
                    if PYMONGO_3:
                        self._get_jobs_collection().insert_one(job_doc)
                    else:
                        self._get_jobs_collection().save(job_doc)
        except FileNotFoundError as error:
            logger.warning(error)
            if not force:
                raise
        try:
            fn_dump_db = os.path.join(src, FN_DUMP_DB)
            if not os.path.exists(fn_dump_db):
                raise NotADirectoryError(fn_dump_db)
            restore_db(self.get_db(), fn_dump_db)
        except NotADirectoryError as error:
            logger.warning(error)
            if not force:
                raise
        for root, dirs, files in os.walk(fn_storage):
            for dir in dirs:
                try:
                    shutil.rmtree(os.path.join(self.filestorage_dir(), dir))
                except (FileNotFoundError, IsADirectoryError):
                    pass
                shutil.move(os.path.join(root, dir), self.filestorage_dir())
            assert os.path.exists(os.path.join(self.filestorage_dir(), dir))
            break
    
    def _restore_snapshot(self, src):
        if os.path.isfile(src):
            with TemporaryDirectory() as tmp_dir:
                with tarfile.open(src, 'r') as file:
                    file.extractall(tmp_dir)
                    self._restore_snapshot_from_src(tmp_dir)
        elif os.path.isdir(src):
            self._restore_snapshot_from_src(src)
        else:
            raise FileNotFoundError(src)

    def _create_snapshot(self, dst, full = True, mode = 'w'):
        with TemporaryDirectory(prefix = 'signac_dump_') as tmp:
            try:
                with tarfile.open(dst, mode) as file:
                    for fn in itertools.chain(
                            self._create_db_snapshot(tmp),
                            self._create_restore_scripts(tmp),
                            self._create_config_snapshot(tmp),
                            self._create_snapshot_view_script(tmp)):
                        logger.debug("Storing '{}'...".format(fn))
                        file.add(fn, os.path.relpath(fn, tmp))
                    if full:
                        for id_ in self._find_job_ids():
                            src_ = os.path.join(self.filestorage_dir(), id_)
                            dst_ = os.path.join(FN_DUMP_STORAGE, id_)
                            file.add(src_, dst_)
            except Exception as error:
                try:
                    os.remove(dst)
                except FileNotFoundError:
                    pass
                raise error

    def create_snapshot(self, dst, full = True):
        fn, ext = os.path.splitext(dst)
        mode = 'w:'
        if ext in ['.gz', '.bz2']:
            mode += ext[1:]
        return self._create_snapshot(dst, full = full, mode = mode)

    def _create_rollbackup(self, dst):
        logger.debug("Creating rollback backup...")
        os.mkdir(dst)
        fn_db_backup = os.path.join(dst, 'db_backup.tar')
        fn_storage_backup = os.path.join(dst, FN_STORAGE_BACKUP)

        self.create_snapshot(fn_db_backup, full = False)
        for job_id in self._find_job_ids():
            try:
                shutil.move(
                    os.path.join(self.filestorage_dir(), job_id),
                    os.path.join(fn_storage_backup, job_id))
            except FileNotFoundError as error:
                pass
    
    def _restore_rollbackup(self, dst):
        fn_storage_backup = os.path.join(dst, FN_STORAGE_BACKUP)
        fn_db_backup = os.path.join(dst, 'db_backup.tar')

        for root, dirs, files in os.walk(fn_storage_backup):
            for dir in dirs:
                try:
                    shutil.rmtree(os.path.join(self.filestorage_dir(), dir))
                except (FileNotFoundError, IsADirectoryError):
                    pass
                shutil.move(os.path.join(root, dir), self.filestorage_dir())
                self._restore_snapshot(fn_db_backup)
    
    def _remove_rollbackup(self, dst):
        shutil.rmtree(dst)

    def restore_snapshot(self, src):
        logger.info("Trying to restore from '{}'.".format(src))
        num_active = self.num_active_jobs()
        if num_active > 0:
            msg = "This project has indication of active jobs. "
            msg += "You can use 'signac cleanup' to remove dead jobs."
            raise RuntimeError(msg.format(num_active))
        self._check_snapshot(src)
        dst_rollbackup = os.path.join(
            self.workspace_dir(),
            'restore_rollback_{}'.format(self.get_id()))
        try:
            self._create_rollbackup(dst_rollbackup)
        except FileExistsError as error:
            raise RollBackupExistsError(dst_rollbackup)
        except BaseException as error:
            try:
                self._remove_rollbackup(str(error))
            except FileNotFoundError:
                pass
            raise
        else:
            try:
                self._restore_snapshot(src)
            except BaseException as error:
                if type(error) == KeyboardInterrupt:
                    print("Interrupted.")
                else:
                    msg = "Error during restore from '{src}': {error}."
                    logger.error(msg.format(src = src, error = error))
                logger.info("Restoring previous state...")
                try:
                    self._restore_rollbackup(dst_rollbackup)
                except:
                    msg = "Failed to rollback!"
                    logger.critical(msg)
                    raise
                else:
                    logger.info("Rolled back.")
                    self._remove_rollbackup(dst_rollbackup)
                raise
            else:
                self._remove_rollbackup(dst_rollbackup)
                logger.info("Restored snapshot '{}'.".format(src))

    def job_pool(self, parameter_set, include = None, exclude = None):
        for p in parameter_set:
            OnlineJob(self, p, timeout = 1)._register_online()
        return JobPool(self, parameter_set, copy.copy(include), copy.copy(exclude))

    @property
    def job_queue_(self):
        if self._job_queue_ is None:
            self._job_queue_ = MongoDBQueue(self.get_db()[COLLECTION_JOB_QUEUE])
        return self._job_queue_

    @property
    def job_queue(self):
        if self._job_queue is None:
            collection_job_results = self.get_db()[COLLECTION_JOB_QUEUE_RESULTS]
            self._job_queue = MongoDBExecutor(self.job_queue_, collection_job_results)
        return self._job_queue

    @property
    def fetched_set(self):
        if self._fetched_set is None:
            self._fetched_set = MongoDBSet(self.get_db()[COLLECTION_FETCHED_SET])
        return self._fetched_set

    def _submit(self, function, *args, **kwargs):
        return self.job_queue.submit(function, *args, **kwargs)

    def submit(self, function, *args, **kwargs):
        f = encode_callable_filter(function, args, kwargs)
        if f in self.fetched_set:
            msg = "Job was already submitted and is currently fetched. Use 'resubmit' to ignore this warning."
            raise ValueError(msg)
        try:
            return self._submit(function, *args, **kwargs)
        except ValueError as error:
            msg = "Job was already submitted and is currently queued. Use 'resubmit' to ignore this warning."
            raise ValueError(msg) from error

    def resubmit(self, function, *args, **kwargs):
        return self._submit(function, args, kwargs)

    def fetched_task_done(self, function, *args, **kwargs):
        f = encode_callable_filter(function, args, kwargs)
        self.fetched_set.remove(f)
        self.job_queue_.task_done()

    def _find_job_docs(self, job_spec, * args, **kwargs):
        yield from self._get_jobs_collection().find(job_spec, *args, **kwargs)
            #self._job_spec_modifier(job_spec), * args, ** kwargs)

    def _walk_job_docs(self, parameters, job_spec = None, * args, ** kwargs):
        yield from self._find_job_docs(
            job_spec = job_spec or dict(),
            sort = [('parameters.{}'.format(p), 1) for p in parameters],
            * args, ** kwargs)

    def dump_db_snapshot(self):
        job_docs = self._get_jobs_collection().find()
        for doc in job_docs:
            print(dumps(doc))
        docs = self.find()
        for doc in docs:
            print(dumps(doc))

    def _get_logging_collection(self):
        return self.get_db()[COLLECTION_LOGGING]

    def clear_logs(self):
        self._get_logging_collection().drop()

    def _logging_db_handler(self, lock_id = None):
        return MongoDBHandler(
            collection = self._get_logging_collection(),
            lock_id = lock_id)

    def logging_handler(self):
        return self._logging_queue_handler

    def start_logging(self, level = logging.INFO):
        for logger in self._loggers:
            logger.addHandler(self.logging_handler())
        if self._logging_listener is None:
            self._logging_listener = QueueListener(
                self._logging_queue, self._logging_db_handler())
        self._logging_listener.start()

    def stop_logging(self):
        self._logging_listener.stop()

    def get_logs(self, level = logging.INFO, limit = 0):
        log_collection = self._get_logging_collection()
        log_collection.create_index('created')
        try:
            levelno = int(level)
        except ValueError:
            levelno = logging.getLevelName(level)
        spec = {'levelno': {'$gte': levelno}}
        sort = [('created', 1)]
        if limit:
            skip = max(0, log_collection.find(spec).count() - limit)
        else:
            skip = 0
        docs = log_collection.find(spec).sort(sort).skip(skip)
        for doc in docs:
            yield record_from_doc(doc)

    def _find_job_ids(self, spec = {}):
        #warnings.warn("Method '_find_job_ids' is under consideration for removal.", PendingDeprecationWarning)
        if PYMONGO_3:
            docs = self._find_job_docs(spec, projection = ['_id'])
        else:
            docs = self._find_job_docs(spec, fields = ['_id'])
        for doc in docs:
            yield doc['_id']

    def _check_version(self):
        """Check the project version

        returns: True if the version matches, otherwise False.
        raises: UserWarning, RuntimeError

        The function will raise a UserWarning if the signac version is higher than the project version.
        The function will raise a RuntimeError if the signac version is less than the project version, because correct behaviour can't be guaranteed in this case.
        """
        version = self.config.get('signac_version', (0,1,0))
        if VERSION_TUPLE < version:
            msg = "The project is configured for signac version {}, but the current signac version is {}. Update signac to use this project."
            raise RuntimeError(msg.format(version, VERSION_TUPLE))
        if VERSION_TUPLE > version:
            msg = "The project is configured for signac version {}, but the current signac version is {}. Execute `signac update` to update your project and get rid of this warning."
            raise UserWarning(msg.format(version, VERSION_TUPLE))
            warnings.warn(msg.format(version, VERSION_TUPLE))
            return False
        return True

    def check_version(self):
        """Check the project version against the signac version.

        returns: True if the version matches, otherwise False.
        raises: RuntimeError

        The function will issue a warning if the signac version is higher than the project version.
        The function will raise a RuntimeError if the signac version is less than the project version, because correct behaviour can't be guaranteed in this case.
        """
        try:
            return self._check_version()
        except UserWarning as warning:
            warnings.warn(warning)


    def get_job(self, job_id, blocking = True, timeout = -1):
        warnings.warn("Method get_job() is deprecated.", DeprecationWarning)
        return self.open_job_from_id(job_id, blocking=blocking, timeout=timeout)

    def _job_spec(self, parameters):
        warnings.warn("Method '_job_spec' is deprecated.", DeprecationWarning)
        spec = dict()
        #if not len(parameters):
        #    msg = "Parameters dictionary cannot be empty!"
        #    raise ValueError(msg)
        if parameters is None:
            parameters = dict()
        spec.update({JOB_PARAMETERS_KEY: parameters})
        if self.develop_mode():
            spec.update({'develop': True})
        warnings.warn("The project id will be removed from the job spec!", UserWarning)
        spec.update({
            'project': self.get_id(),
        })
        return spec

    def _job_spec_modifier(self, job_spec = {}, develop = None):
        raise DeprecationWarning("Method '_job_spec_modifier' is deprecated.")

    def find_job_ids(self, spec = {}):
        warnings.warn("Method 'find_job_ids' is no longer part of the public API.", DeprecationWarning)
        yield from self._find_job_ids(spec=spec)

class Project(OnlineProject):
    pass
