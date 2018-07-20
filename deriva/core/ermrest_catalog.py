import logging
import datetime

from . import urlquote, datapath, DEFAULT_HEADERS, DEFAULT_CHUNK_SIZE, Megabyte, get_transfer_summary
from .deriva_binding import DerivaBinding
from .ermrest_config import CatalogConfig
from . import ermrest_model

class ErmrestCatalogMutationError(Exception):
    pass

_clone_state_url = "tag:isrd.isi.edu,2018:clone-status"

class ErmrestCatalog(DerivaBinding):
    """Persistent handle for an ERMrest catalog.

       Provides basic REST client for HTTP methods on arbitrary
       paths. Caller has to understand ERMrest APIs and compose
       appropriate paths, headers, and/or content.

       Additional utility methods provided for accessing catalog metadata.
    """
    table_schemas = dict()

    def __init__(self, scheme, server, catalog_id, credentials=None, caching=True, session_config=None):
        """Create ERMrest catalog binding.

           Arguments:
             scheme: 'http' or 'https'
             server: server FQDN string
             catalog_id: e.g. '1'
             credentials: credential secrets, e.g. cookie
             caching: whether to retain a GET response cache

        """
        super(ErmrestCatalog, self).__init__(scheme, server, credentials, caching, session_config)
        self._server_uri = "%s/ermrest/catalog/%s" % (
            self._server_uri,
            catalog_id
        )
        self._scheme, self._server, self._catalog_id, self._credentials, self._caching, self._session_config = \
            scheme, server, catalog_id, credentials, caching, session_config

    def latest_snapshot(self):
        """Gets a handle to this catalog's latest snapshot.
        """
        r = self.get('/')
        r.raise_for_status()
        return ErmrestSnapshot(self._scheme, self._server, self._catalog_id, r.json()['snaptime'],
                               self._credentials, self._caching, self._session_config)

    def getCatalogConfig(self):
        return CatalogConfig.fromcatalog(self)

    def getCatalogModel(self):
        return ermrest_model.Model.fromcatalog(self)

    def applyCatalogConfig(self, config):
        return config.apply(self)

    def getCatalogSchema(self):
        path = '/schema'
        r = self.get(path)
        r.raise_for_status()
        return r.json()

    def getPathBuilder(self):
        """Returns the 'path builder' interface for this catalog."""
        return datapath.from_catalog(self)

    def getTableSchema(self, fq_table_name):
        # first try to get from cache(s)
        s, t = self.splitQualifiedCatalogName(fq_table_name)
        cat = self.getCatalogSchema()
        schema = cat['schemas'][s]['tables'][t] if cat else None
        if schema:
            return schema
        schema = self.table_schemas.get(fq_table_name)
        if schema:
            return schema

        path = '/schema/%s/table/%s' % (s, t)
        r = self.get(path)
        resp = r.json()
        self.table_schemas[fq_table_name] = resp
        r.raise_for_status()

        return resp

    def getTableColumns(self, fq_table_name):
        columns = set()
        schema = self.getTableSchema(fq_table_name)
        for column in schema['column_definitions']:
            columns.add(column['name'])

        return columns

    def validateRowColumns(self, row, fq_tableName):
        columns = self.getTableColumns(fq_tableName)
        return set(row.keys()) - columns

    def getDefaultColumns(self, row, table, exclude=None, quote_url=True):
        columns = self.getTableColumns(table)
        if isinstance(exclude, list):
            for col in exclude:
                columns.remove(col)

        defaults = []
        supplied_columns = row.keys()
        for col in columns:
            if col not in supplied_columns:
                defaults.append(urlquote(col, safe='') if quote_url else col)

        return defaults

    @staticmethod
    def splitQualifiedCatalogName(name):
        entity = name.split(':')
        if len(entity) != 2:
            logging.debug("Unable to tokenize %s into a fully qualified <schema:table> name." % name)
            return None
        return entity[0], entity[1]

    def getAsFile(self, path, destfilename, headers=DEFAULT_HEADERS, callback=None):
        """
           Retrieve catalog data streamed to destination file.
           Caller is responsible to clean up file even on error, when the file may or may not be exist.

        """
        self.check_path(path)

        headers = headers.copy()

        destfile = open(destfilename, 'w+b')

        try:
            r = self._session.get(self._server_uri + path, headers=headers, stream=True)
            self._response_raise_for_status(r)

            total = 0
            start = datetime.datetime.now()
            logging.debug("Transferring file %s to %s" % (self._server_uri + path, destfilename))
            for buf in r.iter_content(chunk_size=DEFAULT_CHUNK_SIZE):
                destfile.write(buf)
                total += len(buf)
                if callback:
                    if not callback(progress="Downloading: %.2f MB transferred" % (float(total) / float(Megabyte))):
                        destfile.close()
                        return None
            elapsed = datetime.datetime.now() - start
            summary = get_transfer_summary(total, elapsed)
            logging.info("File [%s] transfer successful. %s" % (destfilename, summary))
            if callback:
                callback(summary=summary, file_path=destfilename)

            return r
        finally:
            destfile.close()

    def delete(self, path, headers=DEFAULT_HEADERS, guard_response=None):
        """Perform DELETE request, returning response object.

           Arguments:
             path: the path within this bound catalog
             headers: headers to set in request
             guard_response: expected current resource state
               as previously seen response object.

           Uses guard_response to build appropriate 'if-match' header
           to assure change is only applied to expected state.

           Raises ConcurrentUpdate for 412 status.

        """
        if path == "/":
            raise DerivaPathError('See self.delete_ermrest_catalog() if you really want to destroy this catalog.')
        return DerivaBinding.delete(self, path, headers=headers, guard_response=guard_response)

    def delete_ermrest_catalog(self, really=False):
        """Perform DELETE request, destroying catalog on server.

           Arguments:
             really: delete when True, abort when False (default)

        """
        if really is True:
            return DerivaBinding.delete('/')
        else:
            raise ValueError('Catalog deletion refused when really is %s.' % really)

    def clone_catalog(self, dst_catalog=None, copy_data=True, copy_annotations=True, copy_policy=True, truncate_after=True):
        """Clone this catalog's content into dest_catalog, creating a new catalog if needed.

        :param dst_catalog: Destination catalog or None to request creation of new destination (default).
        :param copy_data: Copy table contents when True (default).
        :param copy_annotations: Copy annotations when True (default).
        :param copy_policy: Copy access-control policies when True (default).
        :param truncate_after: Truncate destination history after cloning when True (default).

        When dest_catalog is provided, attempt an idempotent clone,
        assuming content MAY be partially cloned already using the
        same parameters. This routine uses a table-level annotation
        "tag:isrd.isi.edu,2018:clone-state" to save progress markers
        which help it restart efficiently if interrupted.

        Cloning preserves source row RID values so that any RID-based
        foreign keys are still valid. It is not generally advisable to
        try to merge more than one source into the same clone, nor to
        clone on top of rows generated locally in the destination,
        since this could cause duplicate RID conflicts.

        Truncation after cloning avoids retaining incremental
        snapshots which contain partial clones.

        """
        src_model = self.getCatalogModel()

        if dst_catalog is None:
            # TODO: refactor with DerivaServer someday
            server = DerivaBinding(self._scheme, self._server, self._credentials, self._caching, self._session_config)
            dst_id = server.post("/ermrest/catalog").json()["id"]
            dst_catalog = ErmrestCatalog(self._scheme, self._server, dst_id, self._credentials, self._caching, self._session_config)

        # set top-level config right away and find fatal usage errors...
        if copy_policy:
            if not src_model.acls:
                raise ValueError("Use of copy_policy=True not possible when caller does not own source catalog.")
            dst_catalog.put('/acl', json=src_model.acls)

        if copy_annotations:
            dst_catalog.put('/annotation', json=src_model.annotations)

        # build up the model content we will copy to destination
        dst_model = dst_catalog.getCatalogModel()

        new_model = []
        clone_states = {}
        fkeys_deferred = {}

        def prune_parts(d):
            if not copy_annotations and 'annotations' in d:
                del d['annotations']
            if not copy_policy:
                if 'acls' in d:
                    del d['acls']
                if 'acl_bindings' in d:
                    del d['acl_bindings']
            return d

        def copy_sdef(s):
            """Copy schema definition structure with conditional parts for cloning."""
            d = prune_parts(s.prejson())
            if 'tables' in d:
                del d['tables']
            return d

        def copy_tdef_core(t):
            """Copy table definition structure with conditional parts excluding fkeys."""
            d = prune_parts(t.prejson())
            d['column_definitions'] = [ prune_parts(c) for c in d['column_definitions'] ]
            d['keys'] = [ prune_parts(c) for c in d.get('keys', []) ]
            if 'foreign_keys' in d:
                del d['foreign_keys']
            if 'annotations' not in d:
                d['annotations'] = {}
                d['annotations'][_clone_state_url] = 1 if copy_data else None
            return d

        def copy_tdef_fkeys(t):
            """Copy table fkeys structure."""
            return [ prune_parts(d) for d in t.prejson().get('foreign_keys', []) ]

        for sname, schema in src_model.schemas.items():
            if sname not in dst_model.schemas:
                new_model.append(copy_sdef(schema))

            for tname, table in schema.tables.items():
                if sname == 'public' and tname == 'ermrest_client':
                    # skip this automagic table...
                    continue

                if 'RID' not in table.column_definitions.elements:
                    raise ValueError("Source table %s.%s lacks system-columns and cannot be cloned." % (sname, tname))

                if sname not in dst_model.schemas or tname not in dst_model.schemas[sname].tables:
                    new_model.append(copy_tdef_core(table))
                    clone_states[(sname, tname)] = 1 if copy_data else None
                    fkeys_deferred[(sname, tname)] = copy_tdef_fkeys(table)
                else:
                    if sorted([ c.name for c in table.column_definitions ]) \
                       != sorted([ c.name for c in dst_model.schemas[sname].tables[tname].column_definitions ]):
                        raise ValueError("Source table %s.%s conflicts with existing definition in destination catalog." % (sname, tname))
                    clone_states[(sname, tname)] = dst_model.schemas[sname].tables[tname].annotations.get(_clone_state_url)
                    if dst_model.schemas[sname].tables[tname].foreign_keys:
                        # assume that presence of any destination foreign keys means we already completed
                        return dst_catalog
                    else:
                        fkeys_deferred[(sname, tname)] = copy_tdef_fkeys(table)

        # apply the stage 1 model to the destination in bulk
        if new_model:
            dst_catalog.post("/schema", json=new_model).raise_for_status()

        # copy data in stage 2
        if copy_data:
            page_size = 10000
            for sname, tname in clone_states.keys():
                if clone_states[(sname, tname)] == 1:
                    # determine current position in (partial?) copy
                    tname_uri = "%s:%s" % (urlquote(sname), urlquote(tname))
                    r = dst_catalog.get("/entity/%s@sort(RID::desc::)?limit=1" % tname_uri).json()
                    if r:
                        last = r[0]['RID']
                    else:
                        last = None

                    while True:
                        page = self.get(
                            "/entity/%s@sort(RID)%s?limit=%d" % (
                                tname_uri,
                                ("@after(%s)" % urlquote(last)) if last is not None else "",
                                page_size
                            )
                        ).json()
                        if page:
                            dst_catalog.post("/entity/%s?nondefaults=RID,RCT,RCB" % tname_uri, json=page)
                            last = page[-1]['RID']
                        else:
                            break

                    # record our progress on catalog in case we fail part way through
                    dst_catalog.put(
                        "/schema/%s/table/%s/annotation/%s" % (
                            urlquote(sname),
                            urlquote(tname),
                            urlquote(_clone_state_url),
                        ),
                        json=2
                    )

        # apply stage 2 model in bulk only... we won't get here unless preceding succeeded
        new_fkeys = []
        for fkeys in fkeys_deferred.values():
            new_fkeys.extend(fkeys)

        if new_fkeys:
            dst_catalog.post("/schema", json=new_fkeys)

        # truncate cloning history
        if truncate_after:
            snaptime = dst_catalog.get("/").json()["snaptime"]
            dst_catalog.delete("/history/,%s" % urlquote(snaptime))

        return dst_catalog

class ErmrestSnapshot(ErmrestCatalog):
    """Persistent handle for an ERMrest catalog snapshot.

    Inherits from ErmrestCatalog and provides the same interfaces,
    except that the interfaces are now bound to a fixed snapshot
    of the catalog.
    """
    def __init__(self, scheme, server, catalog_id, snaptime, credentials=None, caching=True, session_config=None):
        """Create ERMrest catalog snapshot binding.

           Arguments:
             scheme: 'http' or 'https'
             server: server FQDN string
             catalog_id: e.g., '1'
             snaptime: e.g., '2PM-DGYP-56Z4'
             credentials: credential secrets, e.g. cookie
             caching: whether to retain a GET response cache
        """
        super(ErmrestSnapshot, self).__init__(scheme, server, catalog_id, credentials, caching, session_config)
        self._server_uri = "%s@%s" % (
            self._server_uri,
            snaptime
        )
        self._snaptime = snaptime

    @property
    def snaptime(self):
        """The snaptime for this catalog snapshot instance."""
        return self._snaptime

    def _pre_mutate(self, path, headers, guard_response=None):
        """Override and disable mutation operations.

        When called by the super-class, this method raises an exception.
        """
        raise ErmrestCatalogMutationError('Catalog snapshot is immutable')
