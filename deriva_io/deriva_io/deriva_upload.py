import os
import re
import sys
import logging
from deriva_common import ErmrestCatalog, HatracStore, format_exception, urlquote, DEFAULT_HEADERS
from deriva_common.utils import hash_utils as hu, mime_utils as mu

try:
    from os import scandir, walk
except ImportError:
    from scandir import scandir, walk


class DerivaUpload(object):
    """
    Base class for upload tasks. Encapsulates a catalog instance and a hatrac store instance and provides some common
    and reusable functions.

    This class is not intended to be instantiated directly, but rather extended by a deployment specific implementation.
    """
    def __init__(self, config=None, credentials=None):

        protocol = config['server']['protocol']
        server = config['server']['host']
        catalog_id = config['server']['catalog_id']
        session_config = config.get('session')

        self.catalog = ErmrestCatalog(protocol, server, catalog_id, credentials, session_config=session_config)
        self.store = HatracStore(protocol, server, credentials, session_config=session_config)

        self.asset_mappings = config.get('asset_mappings', [])
        mu.add_types(config.get('mime_overrides'))

        self.file_list = dict()
        self.skipped_uploads = set()
        self.failed_uploads = set()

        self.config = config

    def cleanup(self):
        pass

    @staticmethod
    def getFileSize(file_path):
        return os.path.getsize(file_path)

    @staticmethod
    def guessContentType(file_path):
        return mu.guess_content_type(file_path)

    @staticmethod
    def getFileHashes(file_path, hashes=frozenset(['md5'])):
        return hu.compute_file_hashes(file_path, hashes)

    def getFileDisplayName(self, file_path, asset_mapping):
        return os.path.basename(file_path)

    def validateFile(self, root, path, name):
        file_path = os.path.normpath(os.path.join(path, name))
        asset_mapping = self.getAssetMapping(file_path)
        if not asset_mapping:
            logging.info("Skipping file: [%s] -- Invalid file type or directory location." % file_path)
            return None
        else:
            logging.info("Including file: [%s]." % file_path)

        return {file_path: asset_mapping}

    def uploadFile(self, file_path, asset_mapping, callback=None):
        """
        This method should be implemented by subclasses.
        """
        raise NotImplementedError()

    def uploadFiles(self, status_callback=None, file_callback=None):
        for file_path, asset_mapping in self.file_list.items():
            if not self.uploadFile(file_path, asset_mapping, file_callback):
                self.failed_uploads.add(file_path)

    def scanDirectory(self, root, abort_invalid_input=False):
        """

        :param root:
        :param abort_invalid_input:
        :return:
        """
        logging.info("Scanning files in directory [%s]..." % root)
        for path, dirs, files in walk(root):
            for file_name in files:
                file_entry = self.validateFile(root, path, file_name)
                if not file_entry:
                    if abort_invalid_input:
                        logging.error("Invalid input detected, aborting.")
                        return False
                    self.skipped_uploads.add(os.path.normpath(os.path.join(path, file_name)))
                else:
                    self.file_list.update(file_entry)

        return True

    def getAssetMapping(self, file_path):
        """
        :param file_path:
        :return:
        """
        for asset_type in self.asset_mappings:
            dir_pattern = asset_type.get('dir_pattern', '')
            file_pattern = asset_type.get('file_pattern', '')
            ext_pattern = asset_type.get('ext_pattern', '')
            if dir_pattern and not re.search(dir_pattern, file_path.replace("\\", "/")):
                continue
            if ext_pattern and not re.search(ext_pattern, file_path, re.IGNORECASE):
                continue
            if file_pattern and not re.search(file_pattern, file_path):
                continue
            return asset_type

        return None

    def _catalogRecordCreate(self, catalog_table, row, default_columns=None):
        """

        :param catalog_table:
        :param row:
        :param default_columns:
        :return:
        """
        try:
            missing = self.catalog.validateRowColumns(row, catalog_table)
            if missing:
                logging.error(
                    "Unable to update catalog entry because one or more specified columns do not exist in the "
                    "target table: [%s]" % ','.join(missing))
                return False
            if not default_columns:
                default_columns = self.catalog.getDefaultColumns(row, catalog_table)
            default_param = ('?defaults=%s' % ','.join(default_columns)) if len(default_columns) > 0 else ''
            # for default in default_columns:
            #    row[default] = None
            self.catalog.post('/entity/%s%s' % (catalog_table, default_param), json=[row])
            return True
        except:
            (etype, value, traceback) = sys.exc_info()
            logging.error("Unable to update catalog entry: %s" % format_exception(value))
            return False

    def _catalogRecordUpdate(self, catalog_table, old_row, new_row):
        """

        :param catalog_table:
        :param new_row:
        :param old_row:
        :return:
        """
        try:
            keys = sorted(list(new_row.keys()))
            assert keys == sorted(list(old_row.keys()))
            combined_row = {
                'o%d' % i: old_row[keys[i]]
                for i in range(len(keys))
            }
            combined_row.update({
                'n%d' % i: new_row[keys[i]]
                for i in range(len(keys))
            })
            self.catalog.put(
                '/attributegroup/%s/%s;%s' % (
                    catalog_table,
                    ','.join(["o%d:=%s" % (i, urlquote(keys[i])) for i in range(len(keys))]),
                    ','.join(["n%d:=%s" % (i, urlquote(keys[i])) for i in range(len(keys))])
                ),
                json=[combined_row]
            )
            return True
        except:
            (etype, value, traceback) = sys.exc_info()
            logging.error("Unable to update catalog entry: %s" % format_exception(value))
            return False

