# -*- coding: utf-8 -*-

""" S3 Data Objects API

    @copyright: 2009-2014 (c) Sahana Software Foundation
    @license: MIT

    Permission is hereby granted, free of charge, to any person
    obtaining a copy of this software and associated documentation
    files (the "Software"), to deal in the Software without
    restriction, including without limitation the rights to use,
    copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
    OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
    HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
    WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.

    @group Resource API: S3Resource,
    @group Filter API: S3ResourceFilter
    @group Helper Classes: S3RecordMerger
"""

import datetime
import sys

from itertools import chain, groupby

try:
    from cStringIO import StringIO # Faster, where available
except:
    from StringIO import StringIO

try:
    from lxml import etree
except ImportError:
    print >> sys.stderr, "ERROR: lxml module needed for XML handling"
    raise

try:
    import json # try stdlib (Python 2.6)
except ImportError:
    try:
        import simplejson as json # try external module
    except:
        import gluon.contrib.simplejson as json # fallback to pure-Python module

from gluon import current
from gluon.http import HTTP
from gluon.html import TAG
from gluon.validators import IS_EMPTY_OR
from gluon.dal import Row, Rows, Table, Field, Expression
from gluon.storage import Storage
from gluon.tools import callback

from s3data import S3DataTable, S3DataList, S3PivotTable
from s3fields import S3Represent, s3_all_meta_field_names
from s3query import FS, S3ResourceField, S3ResourceQuery, S3LeftJoins, S3URLQuery
from s3utils import s3_has_foreign_key, s3_get_foreign_key, s3_unicode, s3_get_last_record_id, s3_remove_last_record_id
from s3validators import IS_ONE_OF
from s3xml import S3XMLFormat

DEBUG = False
if DEBUG:
    print >> sys.stderr, "S3Resource: DEBUG MODE"
    def _debug(m):
        print >> sys.stderr, m
else:
    _debug = lambda m: None

osetattr = object.__setattr__
ogetattr = object.__getattribute__

MAXDEPTH = 10

# =============================================================================
class S3Resource(object):
    """
        API for resources.

        A "resource" is a set of records in a database table including their
        references in certain related resources (components). A resource can
        be defined like:

            resource = S3Resource(table)

        A resource defined like this would include all records in the table.
        Further parameters for the resource constructor as well as methods
        of the resource instance can be used to filter for particular subsets.

        This API provides extended standard methods to access and manipulate
        data in resources while respecting current authorization and other
        S3 framework rules.
    """

    def __init__(self, tablename,
                 id=None,
                 prefix=None,
                 uid=None,
                 filter=None,
                 vars=None,
                 parent=None,
                 linked=None,
                 linktable=None,
                 alias=None,
                 components=None,
                 filter_component=None,
                 include_deleted=False,
                 approved=True,
                 unapproved=False,
                 context=False):
        """
            Constructor

            @param tablename: tablename, Table, or an S3Resource instance
            @param prefix: prefix to use for the tablename

            @param id: record ID (or list of record IDs)
            @param uid: record UID (or list of record UIDs)

            @param filter: filter query
            @param vars: dictionary of URL query variables

            @param components: list of component aliases
                               to load for this resource
            @param filter_component: alias of the component the URL filters
                                     apply for (filters for this component
                                     must be handled separately)

            @param alias: the alias for this resource (internal use only)
            @param parent: the parent resource (internal use only)
            @param linked: the linked resource (internal use only)
            @param linktable: the link table (internal use only)

            @param include_deleted: include deleted records (used for
                                    synchronization)

            @param approved: include approved records
            @param unapproved: include unapproved records
            @param context: apply context filters
        """

        s3db = current.s3db
        auth = current.auth

        # Names ---------------------------------------------------------------

        self.table = None
        self._alias = None

        if prefix is None:
            if not isinstance(tablename, basestring):
                if isinstance(tablename, Table):
                    self.table = tablename
                    self._alias = self.table._tablename
                    tablename = self._alias
                elif isinstance(tablename, S3Resource):
                    self.table = tablename.table
                    self._alias = self.table._tablename
                    tablename = tablename.tablename
                else:
                    error = "%s is not a valid type for a tablename" % tablename
                    raise SyntaxError(error)
            if "_" in tablename:
                prefix, name = tablename.split("_", 1)
            else:
                raise SyntaxError("invalid tablename: %s" % tablename)
        else:
            name = tablename
            tablename = "%s_%s" % (prefix, name)

        self.prefix = prefix
        """ Module prefix of the tablename """
        self.name = name
        """ Tablename without module prefix """
        self.tablename = tablename
        """ Tablename """
        self.alias = alias or name
        """
            Alias of the resource, defaults to tablename
            without module prefix
        """

        # Table ---------------------------------------------------------------

        if self.table is None:
            self.table = s3db[tablename]
        table = self.table

        # Set default approver
        auth.permission.set_default_approver(table)

        if not self._alias:
            self._alias = tablename
            """ Table alias (the tablename used in joins/queries) """

        if parent is not None:
            if parent.tablename == self.tablename:
                alias = "%s_%s_%s" % (prefix, self.alias, name)
                pkey = table._id.name
                table = table = table.with_alias(alias)
                table._id = table[pkey]
                self._alias = alias
        self.table = table

        self.fields = table.fields
        self._id = table._id

        # Hooks ---------------------------------------------------------------

        # Authorization hooks
        self.accessible_query = auth.s3_accessible_query

        # Filter --------------------------------------------------------------

        # Default query options
        self.include_deleted = include_deleted
        self._approved = approved
        self._unapproved = unapproved

        # Component Filter
        self.filter = None

        # Resource Filter
        self.rfilter = None
        #self.fquery = None
        #self.fvfltr = None

        # Rows ----------------------------------------------------------------

        self._rows = None
        self._rowindex = None
        self.rfields = None
        self.dfields = None
        self._ids = []
        self._uids = []
        self._length = None

        # Request attributes --------------------------------------------------

        self.vars = None # set during build_query
        self.lastid = None
        self.files = Storage()

        # Components ----------------------------------------------------------

        # Initialize component properties (will be set during _attach)
        self.link = None
        self.linktable = None
        self.actuate = None
        self.lkey = None
        self.rkey = None
        self.pkey = None
        self.fkey = None
        self.multiple = True

        self.parent = parent # the parent resource
        self.linked = linked # the linked resource

        self.components = Storage()
        self.links = Storage()

        if parent is None:
            # This is the master resource - attach components
            attach = self._attach
            hooks = s3db.get_components(table, names=components)
            [attach(alias, hooks[alias]) for alias in hooks]

            # Build query
            self.build_query(id=id,
                             uid=uid,
                             filter=filter,
                             vars=vars,
                             filter_component=filter_component)
            if context:
                self.add_filter(s3db.context)

        # Component - attach link table
        elif linktable is not None:
            # This is link-table component - attach the link table
            self.link = S3Resource(linktable,
                                   parent=self.parent,
                                   linked=self,
                                   include_deleted=self.include_deleted,
                                   approved=self._approved,
                                   unapproved=self._unapproved)

        # Export and Import ---------------------------------------------------

        # Pending Imports
        self.skip_import = False
        self.job = None
        self.mtime = None
        self.error = None
        self.error_tree = None
        self.import_count = 0
        self.import_created = []
        self.import_updated = []
        self.import_deleted = []

        # Export meta data
        self.muntil = None      # latest mtime of the exported records
        self.results = None     # number of exported records

        # Standard methods ----------------------------------------------------

        # CRUD
        from s3crud import S3CRUD
        self.crud = S3CRUD()
        self.crud.resource = self

    # -------------------------------------------------------------------------
    def _attach(self, alias, hook):
        """
            Attach a component

            @param alias: the alias
            @param hook: the hook
        """
        
        if alias is not None and hook.filterby is not None:
            table_alias = "%s_%s_%s" % (hook.prefix,
                                        hook.alias,
                                        hook.name)
            table = hook.table.with_alias(table_alias)
            table._id = table[table._id.name]
            hook.table = table
        else:
            table_alias = None

        # Create as resource
        component = S3Resource(hook.table,
                               parent=self,
                               alias=alias,
                               linktable=hook.linktable,
                               include_deleted=self.include_deleted,
                               approved=self._approved,
                               unapproved=self._unapproved)

        if table_alias:
            component.tablename = hook.tablename
            component._alias = table_alias

        # Update component properties
        component.pkey = hook.pkey
        component.fkey = hook.fkey
        component.linktable = hook.linktable
        component.lkey = hook.lkey
        component.rkey = hook.rkey
        component.actuate = hook.actuate
        component.autodelete = hook.autodelete
        component.autocomplete = hook.autocomplete
        component.alias = alias
        component.multiple = hook.multiple
        component.values = hook.values

        if hook.filterby is not None:
            filterfor = hook.filterfor
            is_list = isinstance(filterfor, (tuple, list))
            if is_list and len(filterfor) == 1:
                is_list = False
                filterfor = filterfor[0]
            if not is_list:
                component.filter = (hook.table[hook.filterby] == filterfor)
            elif filterfor:
                component.filter = (hook.table[hook.filterby].belongs(filterfor))
            else:
                component.filter = None
        else:
            component.filter = None    

        # Copy properties to the link
        if component.link is not None:
            link = component.link
            link.pkey = component.pkey
            link.fkey = component.lkey
            link.actuate = component.actuate
            link.autodelete = component.autodelete
            link.multiple = component.multiple
            self.links[link.name] = link

        self.components[alias] = component
        return

    # -------------------------------------------------------------------------
    # Query handling
    # -------------------------------------------------------------------------
    def build_query(self,
                    id=None,
                    uid=None,
                    filter=None,
                    vars=None,
                    filter_component=None):
        """
            Query builder

            @param id: record ID or list of record IDs to include
            @param uid: record UID or list of record UIDs to include
            @param filter: filtering query (DAL only)
            @param vars: dict of URL query variables
            @param filter_component: the alias of the component the URL
                                     filters apply for (filters for this
                                     component must be handled separately)
        """

        # Reset the rows counter
        self._length = None

        self.rfilter = S3ResourceFilter(self,
                                        id=id,
                                        uid=uid,
                                        filter=filter,
                                        vars=vars,
                                        filter_component=filter_component)
        return self.rfilter

    # -------------------------------------------------------------------------
    def add_filter(self, f=None, c=None):
        """
            Extend the current resource filter

            @param f: a Query or a S3ResourceQuery instance
            @param c: alias of the component this filter concerns,
                      automatically adds the respective component join
                      (not needed for S3ResourceQuery instances)
        """

        if f is None:
            return
        self.clear()
        if self.rfilter is None:
            self.rfilter = S3ResourceFilter(self)
        self.rfilter.add_filter(f, component=c)

    # -------------------------------------------------------------------------
    def add_component_filter(self, alias, f=None):
        """
            Extend the resource filter of a particular component, does
            not affect the master resource filter (as opposed to add_filter)

            @param alias: the alias of the component
            @param f: a Query or a S3ResourceQuery instance
        """

        if f is None:
            return
        if self.rfilter is None:
            self.rfilter = S3ResourceFilter(self)
        self.rfilter.add_filter(f, component=alias, master=False)

    # -------------------------------------------------------------------------
    def get_query(self):
        """ Get the effective query """

        if self.rfilter is None:
            self.build_query()
        return self.rfilter.get_query()

    # -------------------------------------------------------------------------
    def get_filter(self):
        """ Get the effective virtual fields filter """

        if self.rfilter is None:
            self.build_query()
        return self.rfilter.get_filter()

    # -------------------------------------------------------------------------
    def clear_query(self):
        """ Removes the current query (does not remove the set!) """

        self.rfilter = None
        components = self.components
        if components:
            for c in components:
                components[c].clear_query()

    # -------------------------------------------------------------------------
    # Data access (new API)
    # -------------------------------------------------------------------------
    def count(self, left=None, distinct=False):
        """
            Get the total number of available records in this resource

            @param left: left outer joins, if required
            @param distinct: only count distinct rows
        """

        if self.rfilter is None:
            self.build_query()
        if self._length is None:
            self._length = self.rfilter.count(left=left,
                                              distinct=distinct)
        return self._length

    # -------------------------------------------------------------------------
    def select(self,
               fields,
               start=0,
               limit=None,
               left=None,
               orderby=None,
               groupby=None,
               distinct=False,
               virtual=True,
               count=False,
               getids=False,
               as_rows=False,
               represent=False,
               show_links=True,
               raw_data=False):
        """
            Extract data from this resource

            @param fields: the fields to extract (selector strings)
            @param start: index of the first record
            @param limit: maximum number of records
            @param left: additional left joins required for filters
            @param orderby: orderby-expression for DAL
            @param groupby: fields to group by (overrides fields!)
            @param distinct: select distinct rows
            @param virtual: include mandatory virtual fields
            @param count: include the total number of matching records
            @param getids: include the IDs of all matching records
            @param as_rows: return the rows (don't extract)
            @param represent: render field value representations
            @param raw_data: include raw data in the result
        """

        # Init
        db = current.db
        s3db = current.s3db
        table = self.table
        tablename = table._tablename
        pkey = str(table._id)
        
        query = self.get_query()
        vfltr = self.get_filter()
        
        rfilter = self.rfilter
        resolve = self.resolve_selectors

        # dict to collect accessible queries for differential
        # field authorization (each joined table is authorized separately)
        aqueries = {} 

        # Query to use for filtering
        filter_query = query

        #if DEBUG:
        #    _start = datetime.datetime.now()
        #    _debug("select of %s starting" % tablename)

        # Resolve tables, fields and joins
        joins = {}
        left_joins = S3LeftJoins(tablename)

        # Left joins from filter
        ftables = left_joins.add(rfilter.get_left_joins())

        # Left joins from caller
        qtables = left_joins.add(left)
        ftables.extend(qtables)

        # Virtual fields and extra fields required by filter
        virtual_fields = rfilter.get_fields()
        vfields, vjoins, l, d = resolve(virtual_fields, show=False)
        joins.update(vjoins)
        vtables = left_joins.extend(l)
        distinct |= d

        # Display fields (fields to include in the result)
        if fields is None:
            fields = [f.name for f in self.readable_fields()]
        dfields, djoins, l, d = resolve(fields, extra_fields=False)
        joins.update(djoins)
        #dtables = left_joins.extend(l)
        distinct |= d

        # Temporarily deactivate (mandatory) virtual fields
        if not virtual:
            vf = table.virtualfields
            osetattr(table, "virtualfields", [])

        # Initialize field data and effort estimates
        field_data = {pkey: ({}, {}, False, False, False)}
        effort = {pkey: 0}
        for dfield in dfields:
            colname = dfield.colname
            effort[colname] = 0
            field_data[colname] = ({}, {},
                                   dfield.tname != self.tablename,
                                   dfield.ftype[:5] == "list:",
                                   dfield.virtual)

        # Resolve ORDERBY
        orderby_aggregate = orderby_fields = None
        
        if orderby:

            if isinstance(orderby, str):
                items = orderby.split(",")
            elif not isinstance(orderby, (list, tuple)):
                items = [orderby]
            else:
                items = orderby

            orderby = []
            orderby_fields = []

            # For GROUPBY id (which we need here for left joins), we need
            # all ORDERBY-fields to appear in an aggregation function, or
            # otherwise the ORDERBY can be ambiguous.
            orderby_aggregate = []
                
            for item in items:

                expression = None
                
                if type(item) is Expression:
                    f = item.first
                    op = item.op
                    if op == db._adapter.AGGREGATE:
                        # Already an aggregation
                        expression = item
                    elif isinstance(f, Field) and op == db._adapter.INVERT:
                        direction = "desc"
                    else:
                        # Other expression - not supported
                        continue
                elif isinstance(item, Field):
                    direction = "asc"
                    f = item
                elif isinstance(item, str):
                    fn, direction = (item.strip().split() + ["asc"])[:2]
                    tn, fn = ([table._tablename] + fn.split(".", 1))[-2:]
                    try:
                        f = db[tn][fn]
                    except (AttributeError, KeyError):
                        continue
                else:
                    continue

                fname = str(f)
                tname = fname.split(".", 1)[0]
                
                if tname != tablename:
                    if tname in left_joins:
                        ftables.append(tname)
                    elif tname in joins:
                        filter_query &= joins[tname]
                    else:
                        # No join found for this field => skip
                        continue
                    
                orderby_fields.append(f)
                if expression is None:
                    expression = f if direction == "asc" else ~f
                    orderby.append(expression)
                    direction = direction.strip().lower()[:3]
                    if fname != pkey:
                        expression = f.min() if direction == "asc" else ~(f.max())
                else:
                    orderby.append(expression)
                orderby_aggregate.append(expression)

        # Initialize master query
        master_query = filter_query
        
        # Ignore limitby if vfltr
        if vfltr is None:
            limitby = self.limitby(start=start, limit=limit)
        else:
            limitby = None
            
        # Filter Query:
        
        ids = None
        page = None
        totalrows = None

        # Get the left joins
        filter_joins = left_joins.as_list(tablenames=ftables,
                                          aqueries=aqueries)

        if getids or count or left_joins:
            if not groupby and not vfltr and \
               (count or limitby or vtables != ftables):

                if getids or left_joins:
                    field = table._id
                    fdistinct = False
                    fgroupby = field
                else:
                    field = table._id.count()
                    fdistinct = True
                    fgroupby = None

                # We don't need virtual fields here, so deactivate
                # even if virtual is True
                if virtual:
                    vf = table.virtualfields
                    osetattr(table, "virtualfields", [])

                # Retrieve the ordered record IDs (or number of rows)
                rows = db(filter_query).select(field,
                                               left=filter_joins,
                                               distinct=fdistinct,
                                               orderby=orderby_aggregate,
                                               groupby=fgroupby,
                                               cacheable=True)
                                               
                # Restore the virtual fields
                if virtual:
                    osetattr(table, "virtualfields", vf)

                if (getids or left_joins):
                    ids = [row[pkey] for row in rows]
                    totalrows = len(ids)
                    if limitby:
                        page = ids[limitby[0]:limitby[1]]
                    else:
                        page = ids
                    # Use simplified master query
                    master_query = table._id.belongs(page)
                    orderby = None
                    limitby = None
                else:
                    totalrows = rows.first()[field]

        # Master Query:
        
        # Add joins for virtual fields
        for join in vjoins.values():
            master_query &= join

        # Determine fields in master query
        mfields = {}
        qfields = {}

        if groupby:
            # Only extract GROUPBY fields (as we don't support aggregates)

            if isinstance(groupby, str):
                items = groupby.split(",")
            elif not isinstance(groupby, (list, tuple)):
                items = [groupby]
            else:
                items = groupby
                
            groupby = []
            gappend = groupby.append
            for item in items:
                tname = None
                if isinstance(item, Field):
                    f = item
                elif isinstance(item, str):
                    fn = item.strip()
                    tname, fn = ([table._tablename] + fn.split(".", 1))[-2:]
                    try:
                        f = db[tname][fn]
                    except (AttributeError, KeyError):
                        continue
                else:
                    continue
                
                gappend(f)
                fname = str(f)
                qfields[fname] = f

                tnames = None
                for dfield in dfields:
                    if dfield.colname == fname:
                        tnames = dfield.left.keys()
                        break
                if not tnames:
                    if not tname:
                        tname = fname.split(".", 1)[0]
                    if tname != tablename:
                        qtables.append(tname)
                else:
                    qtables.extend([tn for tn in tnames if tn != tablename])
                    
            mfields.update(qfields)

        else:
            
            if ids is None and filter_joins:
                qtables = ftables
            qtables.extend(vtables)

            for flist in [dfields, vfields]:
                for rfield in flist:
                    tname = rfield.tname
                    if tname == tablename or as_rows or tname in qtables:
                        colname = rfield.colname
                        if rfield.show:
                            mfields[colname] = True
                        if rfield.field:
                            qfields[colname] = rfield.field
                        if as_rows and \
                           tname != tablename and \
                           tname not in qtables:
                            left = rfield.left
                            if left:
                                for tn in left:
                                    qtables.extend([j.first._tablename
                                                    for j in left[tn]])
                            else:
                                qtables.append(tname)

        if not groupby:
            if distinct and orderby:
                # With DISTINCT, if an ORDERBY-field is not in SELECT, then
                # add it (required by postgresql).
                if orderby:
                    for orderby_field in orderby_fields:
                        fn = str(orderby_field)
                        if fn not in qfields:
                            qfields[fn] = orderby_field

            # Make sure we have the primary key in SELECT
            if pkey not in qfields:
                qfields[pkey] = self._id
            has_id = True

        elif groupby:
            distinct = False
            if orderby:
                orderby = orderby_aggregate
            has_id = pkey in qfields

        # Get left joins
        master_joins = left_joins.as_list(tablenames=qtables,
                                          aqueries=aqueries)

        # Retrieve the master rows
        rows = db(master_query).select(left=master_joins,
                                       distinct=distinct,
                                       groupby=groupby,
                                       orderby=orderby,
                                       limitby=limitby,
                                       cacheable=not as_rows,
                                       *qfields.values())
                                       
        # Restore virtual fields (if they were deactivated before)
        if not virtual:
            osetattr(table, "virtualfields", vf)

        # Apply virtual fields filter :
        if rows and vfltr is not None:

            if count:
                rows = rfilter(rows)
                totalrows = len(rows)
                
                if limit and start is None:
                    start = 0
                if start is not None and limit is not None:
                    rows = Rows(db,
                                records=rows.records[start:start+limit],
                                colnames=rows.colnames,
                                compact=False)
                elif start is not None:
                    rows = Rows(db,
                                records=rows.records[start:],
                                colnames=rows.colnames,
                                compact=False)
                    
            else:
                rows = rfilter(rows, start=start, limit=limit)

            if (getids or left_joins) and has_id:
                ids = list(set([row[pkey] for row in rows]))
                totalrows = len(ids)

        # With GROUPBY, return the grouped rows here:
        if groupby or as_rows:
            return rows

        # Otherwise: initialize output
        output = {"rfields": dfields,
                  "numrows": 0 if totalrows is None else totalrows,
                  "ids": ids}

        if not rows:
            output["rows"] = []
            return output

        # Extract master rows
        records = self.__extract(rows, pkey, mfields.keys(),
                                 join=hasattr(rows[0], tablename),
                                 field_data=field_data,
                                 effort=effort,
                                 represent = represent)

        # Extract the page IDs
        if page is None:
            if ids is None:
                key = self._id
                page = ids = [row[key] for row in rows]
            else:
                page = ids
                
        # Secondary Queries:

        # Always use simplified query which doesn't need left joins
        squery = table._id.belongs(page)

        # Determine tables and fields
        stables = {}
        for dfield in dfields:
            colname = dfield.colname
            if colname in qfields or dfield.tname == tablename:
                continue
            tname = dfield.tname
            if tname not in stables:
                sfields = stables[tname] = {"_left": S3LeftJoins(table)}
            else:
                sfields = stables[tname]
            if colname not in sfields:
                sfields[colname] = dfield.field
                l = dfield.left
                if l:
                    [sfields["_left"].add(l[tn]) for tn in l]

        # Retrieve + extract into records
        for tname in stables:

            stable = stables[tname]

            # Get the extra fields for subtable
            sresource = s3db.resource(tname)
            efields, ejoins, l, d = sresource.resolve_selectors([])

            # Get all left joins for subtable
            tnames = left_joins.extend(l) + stable["_left"].tables
            sjoins = left_joins.as_list(tablenames=tnames,
                                        aqueries=aqueries)
            if not sjoins:
                continue
            del stable["_left"]

            # Get all fields for subtable query
            extract = stable.keys()
            for efield in efields:
                stable[efield.colname] = efield.field
            sfields = [f for f in stable.values() if f]
            if not sfields:
                sfields.append(s3db.table(tname)._id)
            sfields.insert(0, table._id)

            # Retrieve the subtable rows
            rows = db(squery).select(left=sjoins,
                                     distinct=True,
                                     cacheable=True,
                                     *sfields)

            # Extract and merge the data
            records = self.__extract(rows,
                                     pkey,
                                     extract,
                                     records=records,
                                     join=True,
                                     field_data=field_data,
                                     effort=effort,
                                     represent=represent)

        #if DEBUG:
        #    end = datetime.datetime.now()
        #    duration = end - _start
        #    duration = '{:.4f}'.format(duration.total_seconds())
        #    _debug("All data retrieved after %s seconds" % duration)

        # Represent
        NONE = current.messages["NONE"]
        
        results = {}
        for dfield in dfields:
            
            colname = dfield.colname
            fvalues, frecords, joined, list_type, virtual = field_data[colname]

            if represent:

                # Get the renderer
                renderer = dfield.represent
                if not callable(renderer):
                    # @ToDo: Don't convert unformatted numbers to strings
                    renderer = lambda v: s3_unicode(v) if v is not None else NONE

                # Deactivate linkto if so requested
                if not show_links and hasattr(renderer, "linkto"):
                    linkto = renderer.linkto
                    renderer.linkto = None
                else:
                    linkto = None

                per_row_lookup = list_type and \
                                 effort[colname] < len(fvalues) * 30

                # Render all unique values
                if hasattr(renderer, "bulk") and not list_type:
                    per_row_lookup = False
                    fvalues = renderer.bulk(fvalues.keys(), list_type = False)
                elif not per_row_lookup:
                    for value in fvalues:
                        try:
                            text = renderer(value)
                        except:
                            text = s3_unicode(value)
                        fvalues[value] = text

                # Write representations into result
                for record_id in frecords:

                    if record_id not in results:
                        results[record_id] = Storage() \
                                             if not raw_data \
                                             else Storage(_row=Storage())
                                             
                    record = frecords[record_id]
                    result = results[record_id]

                    # List type with per-row lookup?
                    if per_row_lookup:
                        value = record.keys()
                        if None in value and len(value) > 1:
                            value = [v for v in value if v is not None]
                        try:
                            text = renderer(value)
                        except:
                            text = s3_unicode(value)
                        result[colname] = text
                        if raw_data:
                            result["_row"][colname] = value

                    # Single value (master record)
                    elif len(record) == 1 or \
                         not joined and not list_type:
                        value = record.keys()[0]
                        result[colname] = fvalues[value] \
                                          if value in fvalues else NONE
                        if raw_data:
                            result["_row"][colname] = value
                        continue

                    # Multiple values (joined or list-type)
                    else:
                        vlist = []
                        for value in record:
                            if value is None and not list_type:
                                continue
                            value = fvalues[value] \
                                    if value in fvalues else NONE
                            vlist.append(value)

                        # Concatenate multiple values
                        if any([hasattr(v, "xml") for v in vlist]):
                            data = TAG[""](
                                    list(
                                        chain.from_iterable(
                                            [(v, ", ") for v in vlist])
                                        )[:-1]
                                    )
                        else:
                            data = ", ".join([s3_unicode(v) for v in vlist])

                        result[colname] = data
                        if raw_data:
                            result["_row"][colname] = record.keys()

                # Restore linkto
                if linkto is not None:
                    renderer.linkto = linkto

            else:
                for record_id in records:
                    if record_id not in results:
                        result = results[record_id] = Storage()
                    else:
                        result = results[record_id]

                    data = frecords[record_id].keys()
                    if len(data) == 1 and not list_type:
                        data = data[0]
                    result[colname] = data

        #if DEBUG:
        #    end = datetime.datetime.now()
        #    duration = end - _start
        #    duration = '{:.4f}'.format(duration.total_seconds())
        #    _debug("Representation complete after %s seconds" % duration)
        #_debug("select DONE")

        output["rows"] = [results[record_id] for record_id in page]
        return output
        
    # -------------------------------------------------------------------------
    @staticmethod
    def __extract(rows,
                  pkey,
                  columns,
                  join=True,
                  records=None,
                  field_data=None,
                  effort=None,
                  represent=False):
        """
            Helper method for select to extract data from a
            query result.

            @param rows: the rows
            @param pkey: the primary key
            @param columns: the columns to extract
            @param join: the rows are the result of a join query
            @param records: the records dict to merge the data into
            @param field_data: the cumulative field data
            @param effort: estimated effort for list:type representations
            @param represent: collect unique values per field and estimate
                              representation efforts for list:types
        """

        if records is None:
            records = {}
        
        def get(key):
            t, f = key.split(".", 1)
            if join:
                return lambda row, t=t, f=f: ogetattr(ogetattr(row, t), f)
            else:
                return lambda row, f=f: ogetattr(row, f)

        getkey = get(pkey)
        getval = [get(c) for c in columns]

        for k, g in groupby(rows, key=getkey):
            group = list(g)
            record = records.get(k, {})
            for idx, col in enumerate(columns):
                fvalues, frecords, joined, list_type, virtual = field_data[col]
                values = record.get(col, {})
                lazy = False
                for row in group:
                    try:
                        value = getval[idx](row)
                    except AttributeError:
                        _debug("Warning S3Resource.__extract: column %s not in row" % col)
                        value = None
                    if lazy or callable(value):
                        # Lazy virtual field
                        value = value()
                        lazy = True
                    if virtual and not list_type and type(value) is list:
                        # Virtual field that returns a list
                        list_type = True
                    if list_type and value is not None:
                        if represent and value:
                            effort[col] += 30 + len(value)
                        for v in value:
                            if v not in values:
                                values[v] = None
                            if represent and v not in fvalues:
                                fvalues[v] = None
                    else:
                        if value not in values:
                            values[value] = None
                        if represent and value not in fvalues:
                            fvalues[value] = None
                record[col] = values
                if k not in frecords:
                    frecords[k] = record[col]
            records[k] = record

        return records

    # -------------------------------------------------------------------------
    def insert(self, **fields):
        """
            Insert a record into this resource

            @param fields: dict of field/value pairs to insert
        """

        # Check permission
        authorised = current.auth.s3_has_permission("create", self.tablename)
        if not authorised:
            raise IOError("Operation not permitted: INSERT INTO %s" %
                            self.tablename)

        # Insert new record
        record_id = self.table.insert(**fields)

        # Audit
        if record_id:
            record = Storage(fields).update(id=record_id)
            current.audit("create", self.prefix, self.name, form=record)

        return record_id

    # -------------------------------------------------------------------------
    def update(self):

        raise NotImplementedError

    # -------------------------------------------------------------------------
    def delete(self,
               format=None,
               cascade=False,
               replaced_by=None):
        """
            Delete all (deletable) records in this resource

            @param format: the representation format of the request (optional)
            @param cascade: this is a cascade delete (prevents rollbacks/commits)
            @param replaced_by: used by record merger

            @return: number of records deleted
        """
        
        s3db = current.s3db
        
        # Reset error
        self.error = None

        table = self.table
        get_config = self.get_config
        pkey = self._id.name

        # Determine relevant fields
        fields = [pkey]
        add_field = fields.append
        supertables = get_config("super_entity")
        if supertables:
            # Add super-keys (avoids reloading in delete_super)
            if not isinstance(supertables, (list, tuple)):
                supertables = [supertables]
            for sname in supertables:
                stable = s3db.table(sname) \
                         if isinstance(sname, str) else sname
                if stable is None:
                    continue
                key = stable._id.name
                if key in table.fields:
                    add_field(key)
        if "uuid" in table.fields:
            add_field("uuid")

        # Get all rows
        rows = self.select(fields, as_rows=True)
        if not rows:
            # No rows? => that was it already :)
            return 0

        numrows = 0

        db = current.db
        has_permission = current.auth.s3_has_permission
        
        audit = current.audit
        prefix = self.prefix
        name = self.name
        
        define_resource = s3db.resource
        delete_super = s3db.delete_super
        
        DELETED = current.xml.DELETED
        INTEGRITY_ERROR = current.ERROR.INTEGRITY_ERROR
        
        tablename = self.tablename
        
        if current.deployment_settings.get_security_archive_not_delete() and \
           DELETED in table:

            # Find all deletable rows
            references = table._referenced_by
            try:
                rfields = [f for f in references if f.ondelete == "RESTRICT"]
            except AttributeError:
                # older web2py
                references = [db[tn][fn] for tn, fn in references]
                rfields = [f for f in references if f.ondelete == "RESTRICT"]

            # Determine deletable rows
            deletable = set(row[pkey] for row in rows)
            for rfield in rfields:
                if deletable:
                    fn, tn = rfield.name, rfield.tablename
                    rtable = db[tn]
                    query = (rfield.belongs(deletable))
                    if tn == self.tablename:
                        query &= (rfield != rtable._id)
                    if DELETED in rtable:
                        query &= (rtable[DELETED] != True)
                    rrows = db(query).select(rfield)
                    for rrow in rrows:
                        deletable.discard(rrow[fn])

            # Get custom ondelete-cascade
            ondelete_cascade = get_config("ondelete_cascade")

            for row in rows:
                record_id = row[pkey]

                # Check permission to delete this record
                if not has_permission("delete", table, record_id=record_id):
                    continue
                
                error = self.error
                self.error = None

                # Run custom ondelete_cascade first
                if ondelete_cascade:
                    try:
                        callback(ondelete_cascade, row, tablename=tablename)
                    except:
                        # Custom RESTRICT or cascade failure: row not deletable
                        continue
                    if record_id not in deletable:
                        # Check deletability again
                        restricted = False
                        for rfield in rfields:
                            fn, tn = rfield.name, rfield.tablename
                            rtable = db[tn]
                            #rfield = rtable[fn]
                            query = (rfield == record_id)
                            if tn == self.tablename:
                                query &= (rfield != rtable._id)
                            if DELETED in rtable:
                                query &= (rtable[DELETED] != True)
                            rrow = db(query).select(rfield,
                                                    limitby=(0, 1)).first()
                            if rrow:
                                restricted = True
                                break
                        if not restricted:
                            deletable.add(record_id)

                if record_id not in deletable:
                    # Row is not deletable
                    self.error = INTEGRITY_ERROR
                    continue

                # Run automatic ondelete-cascade
                for rfield in references:
                    fn, tn = rfield.name, rfield.tablename
                    rtable = db[tn]
                    query = (rfield == record_id)
                    if tn == self.tablename:
                        query &= (rfield != rtable._id)
                    if rfield.ondelete == "CASCADE":
                        rresource = define_resource(tn,
                                                    filter=query,
                                                    unapproved=True)
                        rresource.delete(cascade=True)
                        if rresource.error:
                            break
                    elif rfield.ondelete == "SET NULL":
                        try:
                            db(query).update(**{fn:None})
                        except:
                            self.error = INTEGRITY_ERROR
                            break
                    elif rfield.ondelete == "SET DEFAULT":
                        try:
                            db(query).update(**{fn:rfield.default})
                        except:
                            self.error = INTEGRITY_ERROR
                            break

                # Unlink all super-records
                if not self.error and not delete_super(table, row):
                    self.error = INTEGRITY_ERROR
                    
                if self.error:
                    # Error in deletion cascade: roll back + skip row
                    if not cascade:
                        db.rollback()
                    continue
                else:
                    # Auto-delete linked records if this was the last link
                    linked = self.linked
                    if linked and self.autodelete and linked.autodelete:
                        rkey = linked.rkey
                        fkey = linked.fkey
                        if rkey in table:
                            query = (table._id == record_id)
                            this = db(query).select(table._id,
                                                    table[rkey],
                                                    limitby=(0, 1)).first()
                            query = (table._id != this[pkey]) & \
                                    (table[rkey] == this[rkey])
                            if DELETED in table:
                                query &= (table[DELETED] != True)
                            remaining = db(query).select(table._id,
                                                         limitby=(0, 1)).first()
                            if not remaining:
                                linked_table = s3db.table(linked.tablename)
                                query = (linked_table[fkey] == this[rkey])
                                linked = define_resource(linked_table,
                                                         filter=query,
                                                         unapproved=True)
                                linked.delete(cascade=True)
                    # Pull back prior error status
                    self.error = error
                    error = None
                    # "Park" foreign keys to resolve constraints, "un-delete"
                    # would then restore any still-valid FKs from this field!
                    fields = dict(deleted=True)
                    if "deleted_fk" in table:
                        record = table[record_id]
                        fk = {}
                        for f in table.fields:
                            if record[f] is not None and \
                               s3_has_foreign_key(table[f]):
                                fk[f] = record[f]
                                fields[f] = None
                            else:
                                continue
                        if fk:
                            fields.update(deleted_fk=json.dumps(fk))
                    # Annotate the replacement record
                    idstr = str(record_id)
                    if replaced_by and idstr in replaced_by and \
                       "deleted_rb" in table.fields:
                        fields.update(deleted_rb=replaced_by[idstr])
                    # Update the row, finally
                    db(table._id == record_id).update(**fields)
                    numrows += 1
                    # Clear session
                    if s3_get_last_record_id(tablename) == record_id:
                        s3_remove_last_record_id(tablename)
                    # Audit
                    audit("delete", prefix, name,
                          record=record_id, representation=format)
                    # On-delete hook
                    ondelete = get_config("ondelete")
                    if ondelete:
                        callback(ondelete, row)
                    # Commit after each row to not have it rolled back by
                    # subsequent cascade errors
                    if not cascade:
                        db.commit()
        else:
            # Hard delete
            for row in rows:
                record_id = row[pkey]
                # Check permission to delete this row
                if not has_permission("delete", table, record_id=record_id):
                    continue
                
                # @ToDo: ondelete_cascade?

                # Delete super-entity
                success = delete_super(table, row)
                if not success:
                    self.error = INTEGRITY_ERROR
                    continue
                # Delete the row
                try:
                    del table[record_id]
                except:
                    # Row is not deletable
                    self.error = INTEGRITY_ERROR
                    continue
                else:
                    # Successfully deleted
                    numrows += 1
                    # Clear session
                    if s3_get_last_record_id(tablename) == record_id:
                        s3_remove_last_record_id(tablename)
                    # Audit
                    audit("delete", prefix, name,
                          record=row[pkey], representation=format)
                    # On-delete hook
                    ondelete = get_config("ondelete")
                    if ondelete:
                        callback(ondelete, row)
                    # Commit after each row to not have it rolled back by
                    # subsequent cascade errors
                    if not cascade:
                        db.commit()

        if numrows == 0 and not deletable:
            # No deletable rows found
            self.error = INTEGRITY_ERROR

        return numrows

    # -------------------------------------------------------------------------
    def approve(self, components=[], approve=True):
        """
            Approve all records in this resource

            @param components: list of component aliases to include, None
                               for no components, empty list for all components
            @param approve: set to approved (False for reset to unapproved)
        """

        db = current.db
        auth = current.auth

        if auth.s3_logged_in():
            user_id = approve and auth.user.id or None
        else:
            return False

        tablename = self.tablename
        table = self._table

        records = self.select([self._id.name], limit=None)
        for record in records["rows"]:

            record_id = record[str(self._id)]

            # Forget any cached permission for this record
            auth.permission.forget(table, record_id)

            if "approved_by" in table.fields:
                dbset = db(table._id == record_id)
                success = dbset.update(approved_by = user_id)
                if not success:
                    current.db.rollback()
                    return False
                else:
                    onapprove = self.get_config("onapprove", None)
                    if onapprove is not None:
                        row = dbset.select(limitby=(0, 1)).first()
                        if row:
                            callback(onapprove, row, tablename=tablename)
            if components is None:
                continue
            for alias in self.components:
                if components and alias not in components:
                    continue
                component = self.components[alias]
                success = component.approve(components=None, approve=approve)
                if not success:
                    current.db.rollback()
                    return False

        return True

    # -------------------------------------------------------------------------
    def reject(self, cascade=False):
        """ Reject (delete) all records in this resource """

        db = current.db
        s3db = current.s3db

        define_resource = s3db.resource
        DELETED = current.xml.DELETED

        INTEGRITY_ERROR = current.ERROR.INTEGRITY_ERROR
        #has_permission = current.auth.s3_has_permission
        #audit = current.audit
        #prefix = self.prefix
        #name = self.name
        tablename = self.tablename
        table = self.table
        pkey = table._id.name

        # Get hooks configuration
        get_config = s3db.get_config
        ondelete = get_config(tablename, "ondelete")
        onreject = get_config(tablename, "onreject")
        ondelete_cascade = get_config(tablename, "ondelete_cascade")

        # Get all rows
        if "uuid" in table.fields:
            rows = self.select([table._id.name, "uuid"], as_rows=True)
        else:
            rows = self.select([table._id.name], as_rows=True)
        if not rows:
            return True

        delete_super = s3db.delete_super

        if DELETED in table:

            references = table._referenced_by

            for row in rows:

                error = self.error
                self.error = None

                # On-delete-cascade
                if ondelete_cascade:
                    callback(ondelete_cascade, row, tablename=tablename)

                # Automatic cascade
                for ref in references:
                    try:
                        tn, fn = ref.tablename, ref.name
                    except:
                        # old web2py < 2.0
                        tn, fn = ref
                    rtable = db[tn]
                    rfield = rtable[fn]
                    query = (rfield == row[pkey])
                    # Ignore RESTRICTs => reject anyway
                    if rfield.ondelete in ("CASCADE", "RESTRICT"):
                        rresource = define_resource(tn, filter=query, unapproved=True)
                        rresource.reject(cascade=True)
                        if rresource.error:
                            break
                    elif rfield.ondelete == "SET NULL":
                        try:
                            db(query).update(**{fn:None})
                        except:
                            self.error = INTEGRITY_ERROR
                            break
                    elif rfield.ondelete == "SET DEFAULT":
                        try:
                            db(query).update(**{fn:rfield.default})
                        except:
                            self.error = INTEGRITY_ERROR
                            break

                if not self.error and not delete_super(table, row):
                    self.error = INTEGRITY_ERROR

                if self.error:
                    db.rollback()
                    raise RuntimeError("Reject failed for %s.%s" %
                                      (tablename, row[table._id]))
                else:
                    # Pull back prior error status
                    self.error = error
                    error = None

                    # On-reject hook
                    if onreject:
                        callback(onreject, row, tablename=tablename)

                    # Park foreign keys
                    fields = dict(deleted=True)
                    if "deleted_fk" in table:
                        record = table[row[pkey]]
                        fk = {}
                        for f in table.fields:
                            if record[f] is not None and \
                               s3_has_foreign_key(table[f]):
                                fk[f] = record[f]
                                fields[f] = None
                            else:
                                continue
                        if fk:
                            fields.update(deleted_fk=json.dumps(fk))

                    # Update the row, finally
                    db(table._id == row[pkey]).update(**fields)

                    # Clear session
                    if s3_get_last_record_id(tablename) == row[pkey]:
                        s3_remove_last_record_id(tablename)

                    # On-delete hook
                    if ondelete:
                        callback(ondelete, row, tablename=tablename)

        else:
            # Hard delete
            for row in rows:

                # On-delete-cascade
                if ondelete_cascade:
                    callback(ondelete_cascade, row, tablename=tablename)

                # On-reject
                if onreject:
                    callback(onreject, row, tablename=tablename)

                try:
                    del table[row[pkey]]
                except:
                    # Row is not deletable
                    self.error = INTEGRITY_ERROR
                    db.rollback()
                    raise
                else:
                    # Clear session
                    if s3_get_last_record_id(tablename) == row[pkey]:
                        s3_remove_last_record_id(tablename)

                    # Delete super-entity
                    delete_super(table, row)

                    # On-delete
                    if ondelete:
                        callback(ondelete, row, tablename=tablename)

        return True

    # -------------------------------------------------------------------------
    def merge(self,
              original_id,
              duplicate_id,
              replace=None,
              update=None,
              main=True):
        """ Merge two records, see also S3RecordMerger.merge """

        return S3RecordMerger(self).merge(original_id,
                                          duplicate_id,
                                          replace=replace,
                                          update=update,
                                          main=main)

    # -------------------------------------------------------------------------
    # Exports
    # -------------------------------------------------------------------------
    def datatable(self,
                  fields=None,
                  start=0,
                  limit=None,
                  left=None,
                  orderby=None,
                  distinct=False,
                  getids=False):
        """
            Generate a data table of this resource

            @param fields: list of fields to include (field selector strings)
            @param start: index of the first record to include
            @param limit: maximum number of records to include
            @param left: additional left joins for DB query
            @param orderby: orderby for DB query
            @param distinct: distinct-flag for DB query
            @param getids: return the record IDs of all records matching the
                           query (used in search to create a filter)

            @return: tuple (S3DataTable, numrows, ids), where numrows represents
                     the total number of rows in the table that match the query;
                     ids is empty unless getids=True
        """

        # Choose fields
        if fields is None:
            fields = [f.name for f in self.readable_fields()]
        selectors = list(fields)

        # Automatically include the record ID
        table = self.table
        if table._id.name not in selectors:
            fields.insert(0, table._id.name)
            selectors.insert(0, table._id.name)

        # Skip representation of IDs in data tables
        id_repr = table._id.represent
        table._id.represent = None

        # Extract the data
        data = self.select(selectors,
                           start=start,
                           limit=limit,
                           orderby=orderby,
                           left=left,
                           distinct=distinct,
                           count=True,
                           getids=getids,
                           represent=True)

        rows = data["rows"]

        # Restore ID representation
        table._id.represent = id_repr

        # Empty table - or just no match?
        empty = False
        if not rows:
            DELETED = current.xml.DELETED
            if DELETED in table:
                query = (table[DELETED] != True)
            else:
                query = (table._id > 0)
            row = current.db(query).select(table._id, limitby=(0, 1)).first()
            if not row:
                empty = True
                
        # Generate the data table
        rfields = data["rfields"]
        dt = S3DataTable(rfields, rows, orderby=orderby, empty=empty)
        
        return dt, data["numrows"], data["ids"]

    # -------------------------------------------------------------------------
    def datalist(self,
                 fields=None,
                 start=0,
                 limit=None,
                 left=None,
                 orderby=None,
                 distinct=False,
                 getids=False,
                 list_id=None,
                 layout=None):
        """
            Generate a data list of this resource

            @param fields: list of fields to include (field selector strings)
            @param start: index of the first record to include
            @param limit: maximum number of records to include
            @param left: additional left joins for DB query
            @param orderby: orderby for DB query
            @param distinct: distinct-flag for DB query
            @param getids: return the record IDs of all records matching the
                           query (used in search to create a filter)
            @param list_id: the list identifier
            @param layout: custom renderer function (see S3DataList.render)

            @return: tuple (S3DataList, numrows, ids), where numrows represents
                     the total number of rows in the table that match the query;
                     ids is empty unless getids=True
        """

        # Choose fields
        if fields is None:
            fields = [f.name for f in self.readable_fields()]
        selectors = list(fields)

        # Automatically include the record ID
        table = self.table
        if table._id.name not in selectors:
            fields.insert(0, table._id.name)
            selectors.insert(0, table._id.name)

        # Extract the data
        data = self.select(selectors,
                           start=start,
                           limit=limit,
                           orderby=orderby,
                           left=left,
                           distinct=distinct,
                           count=True,
                           getids=getids,
                           raw_data=True,
                           represent=True)

        # Generate the data list
        numrows = data["numrows"]
        dl = S3DataList(self,
                        fields,
                        data["rows"],
                        list_id=list_id,
                        start=start,
                        limit=limit,
                        total=numrows,
                        layout=layout)

        return dl, numrows, data["ids"]

    # -------------------------------------------------------------------------
    def pivottable(self, rows, cols, layers, strict=True):
        """
            Generate a pivot table of this resource.

            @param rows: field selector for the rows dimension
            @param cols: field selector for the columns dimension
            @param layers: list of tuples (field selector, method) for
                           the aggregation layers
            @param strict: filter out dimension values which don't match
                           the resource filter

            @return: an S3PivotTable instance

            Supported methods: see S3PivotTable
        """

        return S3PivotTable(self, rows, cols, layers, strict=strict)

    # -------------------------------------------------------------------------
    def json(self,
             fields=None,
             start=0,
             limit=None,
             left=None,
             distinct=False,
             orderby=None):
        """
            Export a JSON representation of the resource.

            @param fields: list of field selector strings
            @param start: index of the first record
            @param limit: maximum number of records
            @param left: list of (additional) left joins
            @param distinct: select only distinct rows
            @param orderby: Orderby-expression for the query

            @return: the JSON (as string), representing a list of
                     dicts with {"tablename.fieldname":"value"}
        """

        data = self.select(fields=fields,
                           start=start,
                           limit=limit,
                           orderby=orderby,
                           left=left,
                           distinct=distinct)["rows"]

        return json.dumps(data)

    # -------------------------------------------------------------------------
    # Data Object API
    # -------------------------------------------------------------------------
    def load(self,
             fields=None,
             skip=None,
             start=None,
             limit=None,
             orderby=None,
             virtual=True,
             cacheable=False):
        """
            Loads records from the resource, applying the current filters,
            and stores them in the instance.

            @param fields: list of field names to include
            @param skip: list of field names to skip
            @param start: the index of the first record to load
            @param limit: the maximum number of records to load
            @param orderby: orderby-expression for the query
            @param virtual: whether to load virtual fields or not
            @param cacheable: don't define Row actions like update_record
                              or delete_record (faster, and the record can
                              be cached)

            @return: the records as list of Rows
        """


        table = self.table
        tablename = self.tablename

        UID = current.xml.UID
        load_uids = hasattr(table, UID)

        if not skip:
            skip = tuple()

        if fields or skip:
            s3 = current.response.s3
            if "all_meta_fields" in s3:
                meta_fields = s3.all_meta_fields
            else:
                meta_fields = s3.all_meta_fields = s3_all_meta_field_names()
            s3db = current.s3db

        # Field selection
        qfields = ([table._id.name, UID])
        append = qfields.append
        for f in table.fields:
            
            if f in ("wkt", "the_geom") and \
               (tablename == "gis_location" or \
                tablename.startswith("gis_layer_shapefile_")):
                    
                # Filter out bulky Polygons
                continue

            if fields or skip:

                # Must include all meta-fields
                if f in meta_fields:
                    append(f)
                    continue

                # Must include all super-keys
                ktablename = s3_get_foreign_key(table[f], m2m=False)[0]
                if ktablename:
                    ktable = s3db.table(ktablename)
                    if ktable and hasattr(ktable, "instance_type"):
                        append(f)
                        continue

            if f in skip:
                continue
            if not fields or f in fields:
                qfields.append(f)

        fields = list(set(filter(lambda f: hasattr(table, f), qfields)))

        if self._rows is not None:
            self.clear()

        rfilter = self.rfilter
        multiple = rfilter.multiple if rfilter is not None else True
        if not multiple and self.parent and self.parent.count() == 1:
            start = 0
            limit = 1

        rows = self.select(fields,
                           start=start,
                           limit=limit,
                           orderby=orderby,
                           virtual=virtual,
                           as_rows=True)

        ids = self._ids = []
        new_id = ids.append
        
        self._uids = []
        new_uid = self._uids.append
        self._rows = []
        new_row = self._rows.append
        
        if rows:
            pkey = table._id.name
            for row in rows:
                if hasattr(row, tablename):
                    _row = ogetattr(row, tablename)
                    if type(_row) is Row:
                        row = _row
                record_id = ogetattr(row, pkey)
                if record_id not in ids:
                    new_id(record_id)
                    new_row(row)
                    if load_uids:
                        new_uid(ogetattr(row, UID))
            self._length = len(self._rows)

        return self._rows

    # -------------------------------------------------------------------------
    def clear(self):
        """ Removes the records currently stored in this instance """

        self._rows = None
        self._rowindex = None
        self._length = None
        self._ids = None
        self._uids = None
        self.files = Storage()

        if self.components:
            for c in self.components:
                self.components[c].clear()

    # -------------------------------------------------------------------------
    def records(self, fields=None):
        """
            Get the current set as Rows instance

            @param fields: the fields to include (list of Fields)
        """

        if fields is None:
            if self.tablename == "gis_location":
                fields = [f for f in self.table
                          if f.name not in ("wkt", "the_geom")]
            else:
                fields = [f for f in self.table]

        if self._rows is None:
            return Rows(current.db)
        else:
            colnames = map(str, fields)
            return Rows(current.db, self._rows, colnames=colnames)

    # -------------------------------------------------------------------------
    def __getitem__(self, key):
        """
            Find a record currently stored in this instance by its record ID

            @param key: the record ID
            @return: a Row

            @raises: IndexError if the record is not currently loaded
        """

        index = self._rowindex
        if index is None:
            _id = self._id.name
            rows = self._rows
            if rows:
                index = Storage([(str(row[_id]), row) for row in rows])
            else:
                index = Storage()
            self._rowindex = index
        key = str(key)
        if key in index:
            return index[key]
        raise IndexError

    # -------------------------------------------------------------------------
    def __iter__(self):
        """
            Iterate over the records currently stored in this instance
        """

        if self._rows is None:
            self.load()
        rows = self._rows
        for i in xrange(len(rows)):
            yield rows[i]
        return

    # -------------------------------------------------------------------------
    def get(self, key, component=None, link=None):
        """
            Get component records for a record currently stored in this
            instance.

            @param key: the record ID
            @param component: the name of the component
            @param link: the name of the link table
            
            @return: a Row (if component is None) or a list of rows
        """

        NOT_FOUND = KeyError("Record not found")

        if not key:
            raise NOT_FOUND
        if self._rows is None:
            self.load()
        try:
            master = self[key]
        except IndexError:
            raise NOT_FOUND

        if not component and not link:
            return master
        elif link:
            if link in self.links:
                c = self.links[link]
            else:
                raise AttributeError("Undefined link %s" % link)
        else:
            if component in self.components:
                c = self.components[component]
            else:
                raise AttributeError("Undefined component %s" % component)
        rows = c._rows
        if rows is None:
            rows = c.load()
        if not rows:
            return []
        pkey, fkey = c.pkey, c.fkey
        if pkey in master:
            master_id = master[pkey]
            if c.link:
                lkey, rkey = c.lkey, c.rkey
                lids = [r[rkey] for r in c.link if master_id == r[lkey]]
                rows = [record for record in rows if record[fkey] in lids]
            else:
                rows = [record for record in rows if master_id == record[fkey]]
        else:
            rows = []
        return rows

    # -------------------------------------------------------------------------
    def get_id(self):
        """ Get the IDs of all records currently stored in this instance """

        if self._ids is None:
            self.__load_ids()

        if not self._ids:
            return None
        elif len(self._ids) == 1:
            return self._ids[0]
        else:
            return self._ids

    # -------------------------------------------------------------------------
    def get_uid(self):
        """ Get the UUIDs of all records currently stored in this instance """

        if current.xml.UID not in self.table.fields:
            return None
        if self._ids is None:
            self.__load_ids()

        if not self._uids:
            return None
        elif len(self._uids) == 1:
            return self._uids[0]
        else:
            return self._uids

    # -------------------------------------------------------------------------
    def __len__(self):
        """
            The number of currently loaded rows
        """

        if self._rows is not None:
            return len(self._rows)
        else:
            return 0

    # -------------------------------------------------------------------------
    def __load_ids(self):
        """ Loads the IDs/UIDs of all records matching the current filter """

        table = self.table
        UID = current.xml.UID

        pkey = table._id.name

        if UID in table.fields:
            has_uid = True
            fields = (pkey, UID)
        else:
            has_uid = False
            fields = (pkey, )

        rfilter = self.rfilter
        multiple = rfilter.multiple if rfilter is not None else True
        if not multiple and self.parent and self.parent.count() == 1:
            start = 0
            limit = 1
        else:
            start = limit = None

        rows = self.select(fields,
                           start=start,
                           limit=limit)["rows"]

        if rows:
            ID = str(table._id)
            self._ids = [row[ID] for row in rows]
            if has_uid:
                uid = str(table[UID])
                self._uids = [row[uid] for row in rows]
        else:
            self._ids = []

        return

    # -------------------------------------------------------------------------
    # Representation
    # -------------------------------------------------------------------------
    def __repr__(self):
        """
            String representation of this resource
        """

        pkey = self.table._id.name

        if self._rows:
            ids = [r[pkey] for r in self]
            return "<S3Resource %s %s>" % (self.tablename, ids)
        else:
            return "<S3Resource %s>" % self.tablename

    # -------------------------------------------------------------------------
    def __contains__(self, item):
        """
            Tests whether this resource contains a (real) field.

            @param item: the field selector or Field instance
        """

        fn = str(item)
        if "." in fn:
            tn, fn = fn.split(".", 1)
            if tn == self.tablename:
                item = fn
        try:
            rf = self.resolve_selector(str(item))
        except (SyntaxError, AttributeError):
            return 0
        if rf.field is not None:
            return 1
        else:
            return 0

    # -------------------------------------------------------------------------
    def __nonzero__(self):
        """
            Boolean test of this resource
        """

        return self is not None

    # -------------------------------------------------------------------------
    # XML Export
    # -------------------------------------------------------------------------
    def export_xml(self,
                   start=None,
                   limit=None,
                   msince=None,
                   fields=None,
                   dereference=True,
                   maxdepth=MAXDEPTH,
                   mcomponents=[],
                   rcomponents=None,
                   references=None,
                   stylesheet=None,
                   as_tree=False,
                   as_json=False,
                   maxbounds=False,
                   filters=None,
                   pretty_print=False,
                   **args):
        """
            Export this resource as S3XML

            @param start: index of the first record to export (slicing)
            @param limit: maximum number of records to export (slicing)
            @param msince: export only records which have been modified
                            after this datetime
            @param dereference: include referenced resources
            @param mcomponents: components of the master resource to
                                include (list of tablenames), empty list
                                for all
            @param rcomponents: components of referenced resources to
                                include (list of tablenames), empty list
                                for all
            @param stylesheet: path to the XSLT stylesheet (if required)
            @param as_tree: return the ElementTree (do not convert into string)
            @param as_json: represent the XML tree as JSON
            @param filters: additional URL filters (Sync), as dict
                            {tablename: {url_var: string}}
            @param pretty_print: insert newlines/indentation in the output
            @param args: dict of arguments to pass to the XSLT stylesheet
        """

        xml = current.xml

        output = None
        args = Storage(args)

        xmlformat = S3XMLFormat(stylesheet) if stylesheet else None

        # Export as element tree
        #if DEBUG:
            #_start = datetime.datetime.now()
            #tablename = self.tablename
            #_debug("export_tree of %s starting" % tablename)
        tree = self.export_tree(start=start,
                                limit=limit,
                                msince=msince,
                                fields=fields,
                                dereference=dereference,
                                maxdepth=maxdepth,
                                mcomponents=mcomponents,
                                rcomponents=rcomponents,
                                references=references,
                                filters=filters,
                                maxbounds=maxbounds,
                                xmlformat=xmlformat)
        #if DEBUG:
            #end = datetime.datetime.now()
            #duration = end - _start
            #duration = '{:.2f}'.format(duration.total_seconds())
            #_debug("export_tree of %s completed in %s seconds" % \
                    #(tablename, duration))

        # XSLT transformation
        if tree and xmlformat is not None:
            #if DEBUG:
            #    _start = datetime.datetime.now()
            import uuid
            tfmt = xml.ISOFORMAT
            args.update(domain=xml.domain,
                        base_url=current.response.s3.base_url,
                        prefix=self.prefix,
                        name=self.name,
                        utcnow=datetime.datetime.utcnow().strftime(tfmt),
                        msguid=uuid.uuid4().urn)
            tree = xmlformat.transform(tree, **args)
            #if DEBUG:
                #end = datetime.datetime.now()
                #duration = end - _start
                #duration = '{:.2f}'.format(duration.total_seconds())
                #_debug("transform of %s using %s completed in %s seconds" % \
                        #(tablename, stylesheet, duration))

        # Convert into the requested format
        # (Content Headers are set by the calling function)
        if tree:
            if as_tree:
                output = tree
            elif as_json:
                #if DEBUG:
                    #_start = datetime.datetime.now()
                output = xml.tree2json(tree, pretty_print=pretty_print)
                #if DEBUG:
                    #end = datetime.datetime.now()
                    #duration = end - _start
                    #duration = '{:.2f}'.format(duration.total_seconds())
                    #_debug("tree2json of %s completed in %s seconds" % \
                            #(tablename, duration))
            else:
                output = xml.tostring(tree, pretty_print=pretty_print)

        return output

    # -------------------------------------------------------------------------
    def export_tree(self,
                    start=0,
                    limit=None,
                    msince=None,
                    fields=None,
                    references=None,
                    dereference=True,
                    maxdepth=MAXDEPTH,
                    mcomponents=None,
                    rcomponents=None,
                    filters=None,
                    maxbounds=False,
                    xmlformat=None):
        """
            Export the resource as element tree

            @param start: index of the first record to export
            @param limit: maximum number of records to export
            @param msince: minimum modification date of the records
            @param fields: data fields to include (default: all)
            @param references: foreign keys to include (default: all)
            @param dereference: also export referenced records
            @param mcomponents: components of the master resource to
                                include (list of tablenames), empty list
                                for all
            @param rcomponents: components of referenced resources to
                                include (list of tablenames), empty list
                                for all
            @param filters: additional URL filters (Sync), as dict
                            {tablename: {url_var: string}}
            @param maxbounds: include lat/lon boundaries in the top
                              level element (off by default)
        """

        xml = current.xml

        if xml.show_urls:
            base_url = current.response.s3.base_url
        else:
            base_url = None

        # Split reference/data fields
        (rfields, dfields) = self.split_fields(data=fields,
                                               references=references)

        # Filter for MCI >= 0 (setting)
        table = self.table
        if xml.filter_mci and "mci" in table.fields:
            mci_filter = (table.mci >= 0)
            self.add_filter(mci_filter)

        # Sync filters
        tablename = self.tablename
        if filters and tablename in filters:
            queries = S3URLQuery.parse(self, filters[tablename])
            [self.add_filter(q) for a in queries for q in queries[a]]

        # Initialize export metadata
        self.muntil = None
        self.results = 0

        # Load slice
        if msince is not None and "modified_on" in table.fields:
            orderby = "%s ASC" % table["modified_on"]
        else:
            orderby = None

        # Fields to load
        if xmlformat:
            include, exclude = xmlformat.get_fields(self.tablename)
        else:
            include, exclude = None, None
        self.load(fields=include,
                  skip=exclude,
                  start=start,
                  limit=limit,
                  orderby=orderby,
                  virtual=False,
                  cacheable=True)

        # Total number of results
        results = self.count()

        format = current.auth.permission.format
        if format == "geojson":
            if results > current.deployment_settings.get_gis_max_features():
                headers = {"Content-Type": "application/json"}
                message = "Too Many Records"
                status = 509
                raise HTTP(status,
                           body=xml.json_message(success=False,
                                                 statuscode=status,
                                                 message=message),
                           web2py_error=message,
                           **headers)
            # Lookups per layer not per record
            if tablename == "gis_layer_shapefile":
                # GIS Shapefile Layer
                location_data = current.gis.get_shapefile_geojson(self) or {}
            elif tablename == "gis_theme_data":
                # GIS Theme Layer
                location_data = current.gis.get_theme_geojson(self) or {}
            else:
                # e.g. GIS Feature Layer
                # e.g. Search results
                location_data = current.gis.get_location_data(self) or {}
        elif format in ("georss", "kml", "gpx"):
            location_data = current.gis.get_location_data(self) or {}
        else:
            # @ToDo: Bulk lookup of LatLons for S3XML LatLon-encode
            location_data = {}

        # Build the tree
        #if DEBUG:
        #    _start = datetime.datetime.now()

        root = etree.Element(xml.TAG.root)

        export_map = Storage()
        all_references = []

        prefix = self.prefix
        name = self.name
        if base_url:
            url = "%s/%s/%s" % (base_url, prefix, name)
        else:
            url = "/%s/%s" % (prefix, name)

        # Use lazy representations
        lazy = []
        current.auth_user_represent = S3Represent(lookup="auth_user",
                                                  fields=["email"])

        export_resource = self.__export_resource

        # Collect all references from master records
        reference_map = []
        for record in self._rows:
            element = export_resource(record,
                                      rfields=rfields,
                                      dfields=dfields,
                                      parent=root,
                                      base_url=url,
                                      reference_map=reference_map,
                                      export_map=export_map,
                                      lazy=lazy,
                                      components=mcomponents,
                                      filters=filters,
                                      msince=msince,
                                      location_data=location_data,
                                      xmlformat=xmlformat)
            if element is None:
                results -= 1

        if reference_map:
            all_references.extend(reference_map)
            
        #if DEBUG:
        #    end = datetime.datetime.now()
        #    duration = end - _start
        #    duration = '{:.2f}'.format(duration.total_seconds())
        #    _debug("export_resource of primary resource and components completed in %s seconds" % \
        #        duration)

        # Add referenced resources to the tree
        #if DEBUG:
        #    _start = datetime.datetime.now()

        define_resource = current.s3db.resource

        # Iteratively resolve all references
        depth = maxdepth if dereference else 0
        while reference_map and depth:
            depth -= 1
            load_map = dict()
            get_exported = export_map.get
            for ref in reference_map:
                if "table" in ref and "id" in ref:
                    # Get tablename and IDs
                    tname = ref["table"]
                    ids = ref["id"]
                    if not isinstance(ids, list):
                        ids = [ids]

                    # Exclude records which are already in the tree
                    exported = get_exported(tname, [])
                    ids = [x for x in ids if x not in exported]
                    if not ids:
                        continue

                    # Append the new ids to load_map[tname]
                    if tname in load_map:
                        ids = [x for x in ids if x not in load_map[tname]]
                        load_map[tname] += ids
                    else:
                        load_map[tname] = ids

            # Collect all references from the referenced records
            reference_map = []
            
            REF = xml.ATTRIBUTE.ref
            for tablename in load_map:
                load_list = load_map[tablename]

                # Sync filters
                if filters:
                    filter_vars = filters.get(tablename, None)
                else:
                    filter_vars = None

                prefix, name = tablename.split("_", 1)
                rresource = define_resource(tablename,
                                            id=load_list,
                                            components=[],
                                            vars=filter_vars)
                table = rresource.table
                if base_url:
                    url = "%s/%s/%s" % (base_url, prefix, name)
                else:
                    url = "/%s/%s" % (prefix, name)
                rfields, dfields = rresource.split_fields(data=fields,
                                                          references=references)

                # Fields to load
                if xmlformat:
                    include, exclude = xmlformat.get_fields(rresource.tablename)
                else:
                    include, exclude = None, None
                rresource.load(fields=include,
                               skip=exclude,
                               limit=None,
                               virtual=False,
                               cacheable=True)

                export_resource = rresource.__export_resource
                for record in rresource:
                    element = export_resource(record,
                                              rfields=rfields,
                                              dfields=dfields,
                                              parent=root,
                                              base_url=url,
                                              reference_map=reference_map,
                                              export_map=export_map,
                                              components=rcomponents,
                                              lazy=lazy,
                                              filters=filters,
                                              master=False,
                                              location_data=location_data,
                                              xmlformat=xmlformat)

                    # Mark as referenced element (for XSLT)
                    if element is not None:
                        element.set(REF, "True")
                if reference_map:
                    all_references.extend(reference_map)

        #if DEBUG:
        #    end = datetime.datetime.now()
        #    duration = end - _start
        #    duration = '{:.2f}'.format(duration.total_seconds())
        #    _debug("export_resource of referenced resources and their components completed in %s seconds" % \
        #           duration)

        # Render all pending lazy representations
        if lazy:
            for renderer, element, attr, f in lazy:
                renderer.render_node(element, attr, f)

        # Add Lat/Lon attributes to all location references
        if all_references:
            xml.latlon(all_references)

        # Complete the tree
        tree = xml.tree(None,
                        root=root,
                        domain=xml.domain,
                        url=base_url,
                        results=results,
                        start=start,
                        limit=limit,
                        maxbounds=maxbounds)

        # Store number of results
        self.results = results

        return tree

    # -------------------------------------------------------------------------
    def __export_resource(self,
                          record,
                          rfields=[],
                          dfields=[],
                          parent=None,
                          base_url=None,
                          reference_map=None,
                          export_map=None,
                          lazy=None,
                          components=None,
                          filters=None,
                          msince=None,
                          master=True,
                          location_data=None,
                          xmlformat=None):
        """
            Add a <resource> to the element tree

            @param record: the record
            @param rfields: list of reference fields to export
            @param dfields: list of data fields to export
            @param parent: the parent element
            @param base_url: the base URL of the resource
            @param reference_map: the reference map of the request
            @param export_map: the export map of the request
            @param components: list of components to include from referenced
                               resources (tablenames)
            @param filters: additional URL filters (Sync), as dict
                            {tablename: {url_var: string}}
            @param msince: the minimum update datetime for exported records
            @param master: True of this is the master resource
            @param location_data: the location_data for GIS encoding
        """

        xml = current.xml

        pkey = self.table._id

        # Construct the record URL
        if base_url:
            record_url = "%s/%s" % (base_url, record[pkey])
        else:
            record_url = None

        # Export the record
        add = False
        export = self._export_record
        element, rmap = export(record,
                               rfields=rfields,
                               dfields=dfields,
                               parent=parent,
                               export_map=export_map,
                               lazy=lazy,
                               url=record_url,
                               msince=msince,
                               master=master,
                               location_data=location_data)
                               
        if element is not None:
            add = True

        # Export components
        if components is not None:

            resource_components = self.components.values()
            unfiltered = [c for c in resource_components if c.filter is None]
            
            for component in resource_components:
                ctablename = component.tablename
                    
                # Shall this component be included?
                if components and ctablename not in components:
                    continue

                # We skip a filtered component if an unfiltered
                # component of the same table is available:
                if component.filter is not None and ctablename in unfiltered:
                    continue

                cpkey = component.table._id

                if component.link is not None:
                    c = component.link
                    calias = None
                    lalias = c.alias
                else:
                    c = component
                    calias = c.alias
                    lalias = None

                # Before loading the component: add filters
                if c._rows is None:
                    
                    # MCI filter
                    ctable = c.table
                    if xml.filter_mci and xml.MCI in ctable.fields:
                        mci_filter = FS(xml.MCI) >= 0
                        c.add_filter(mci_filter)

                    # Sync filters
                    ctablename = c.tablename
                    if filters and ctablename in filters:
                        queries = S3URLQuery.parse(self, filters[ctablename])
                        [c.add_filter(q) for a in queries for q in queries[a]]

                    # Fields to load
                    if xmlformat:
                        include, exclude = xmlformat.get_fields(c.tablename)
                    else:
                        include, exclude = None, None

                    # Load the records
                    c.load(fields=include,
                           skip=exclude,
                           limit=None,
                           virtual=False,
                           cacheable=True)

                # Split fields
                crfields, cdfields = c.split_fields(skip=[c.fkey])

                # Construct the component base URL
                if record_url:
                    component_url = "%s/%s" % (record_url, c.alias)
                else:
                    component_url = None

                # Find related records
                crecords = self.get(record[pkey],
                                    component = calias,
                                    link = lalias,
                                    )
                # @todo: load() should limit this automatically:
                if not c.multiple and len(crecords):
                    crecords = [crecords[0]]

                # Export records
                export = c._export_record
                map_record = c.__map_record
                for crecord in crecords:
                    # Construct the component record URL
                    if component_url:
                        crecord_url = "%s/%s" % (component_url, crecord[cpkey])
                    else:
                        crecord_url = None
                    # Export the component record
                    celement, crmap = export(crecord,
                                             rfields=crfields,
                                             dfields=cdfields,
                                             parent=element,
                                             export_map=export_map,
                                             lazy=lazy,
                                             url=crecord_url,
                                             msince=msince,
                                             master=False,
                                             location_data=location_data)
                    if celement is not None:
                        add = True # keep the parent record

                        # Update "modified until" from component
                        if not self.muntil or \
                           c.muntil and c.muntil > self.muntil:
                            self.muntil = c.muntil

                        map_record(crecord, crmap,
                                   reference_map, export_map)

        # Update reference_map and export_map
        if add:
            self.__map_record(record, rmap, reference_map, export_map)
        elif parent is not None and element is not None:
            idx = parent.index(element)
            if idx:
                del parent[idx]
            return None

        return element

    # -------------------------------------------------------------------------
    def _export_record(self,
                       record,
                       rfields=[],
                       dfields=[],
                       parent=None,
                       export_map=None,
                       lazy=None,
                       url=None,
                       msince=None,
                       master=True,
                       location_data=None):
        """
            Exports a single record to the element tree.

            @param record: the record
            @param rfields: list of foreign key fields to export
            @param dfields: list of data fields to export
            @param parent: the parent element
            @param export_map: the export map of the current request
            @param url: URL of the record
            @param msince: minimum last update time
            @param master: True if this is a record in the master resource
            @param location_data: the location_data for GIS encoding
        """

        xml = current.xml

        tablename = self.tablename
        table = self.table

        # Replace user ID representation by lazy method
        auth_user_represent = Storage()
        if hasattr(current, "auth_user_represent"):
            user_ids = ("created_by", "modified_by", "owned_by_user")
            for fn in user_ids:
                if hasattr(table, fn):
                    f = ogetattr(table, fn)
                    auth_user_represent[fn] = f.represent
                    f.represent = current.auth_user_represent

        #postprocess = s3db.get_config(tablename, "onexport", None)

        default = (None, None)

        # Do not export the record if it already is in the export map
        if tablename in export_map and record[table._id] in export_map[tablename]:
            return default

        # Do not export the record if it hasn't been modified since msince
        # NB This can't be moved to tree level as we do want to export records
        #    which have modified components
        MTIME = xml.MTIME
        if MTIME in record:
            if msince is not None and record[MTIME] <= msince:
                return default
            if not self.muntil or record[MTIME] > self.muntil:
                self.muntil = record[MTIME]

        # Audit read
        current.audit("read", self.prefix, self.name,
                      record=record[table._id], representation="xml")

        # Reference map for this record
        rmap = xml.rmap(table, record, rfields)

        # Use alias if distinct from resource name
        linked = self.linked
        if self.parent is not None and linked is not None:
            alias = linked.alias
            name = linked.name
        else:
            alias = self.alias
            name = self.name
        if alias == name:
            alias = None

        postprocess = self.get_config("xml_post_render")

        # Generate the element
        element = xml.resource(parent, table, record,
                               fields=dfields,
                               alias=alias,
                               lazy=lazy,
                               url=url,
                               postprocess=postprocess)

        # Add the references
        xml.add_references(element, rmap,
                           show_ids=current.xml.show_ids, lazy=lazy)

        if master:
            # GIS-encode the element
            # @ToDo: Do this 1/tree not 1/record
            xml.gis_encode(self, record, element, location_data=location_data)

        # Restore normal user_id representations
        for fn in auth_user_represent:
            ogetattr(table, fn).represent = auth_user_represent[fn]

        return (element, rmap)

    # -------------------------------------------------------------------------
    def __map_record(self, record, rmap, reference_map, export_map):
        """
            Add the record to the export map, and update the
            reference map with the record's references

            @param record: the record
            @param rmap: the reference map of the record
            @param reference_map: the reference map of the request
            @param export_map: the export map of the request
        """

        tablename = self.tablename
        record_id = record[self.table._id]

        if rmap:
            reference_map.extend(rmap)
        if tablename in export_map:
            export_map[tablename].append(record_id)
        else:
            export_map[tablename] = [record_id]
        return

    # -------------------------------------------------------------------------
    # XML Import
    # -------------------------------------------------------------------------
    def import_xml(self, source,
                   files=None,
                   id=None,
                   format="xml",
                   stylesheet=None,
                   extra_data=None,
                   ignore_errors=False,
                   job_id=None,
                   commit_job=True,
                   delete_job=False,
                   strategy=None,
                   update_policy=None,
                   conflict_policy=None,
                   last_sync=None,
                   onconflict=None,
                   **args):
        """
            XML Importer

            @param source: the data source, accepts source=xxx, source=[xxx, yyy, zzz] or
                           source=[(resourcename1, xxx), (resourcename2, yyy)], where the
                           xxx has to be either an ElementTree or a file-like object
            @param files: attached files (None to read in the HTTP request)
            @param id: ID (or list of IDs) of the record(s) to update (performs only update)
            @param format: type of source = "xml", "json" or "csv"
            @param stylesheet: stylesheet to use for transformation
            @param extra_data: for CSV imports, dict of extra cols to add to each row
            @param ignore_errors: skip invalid records silently
            @param job_id: resume from previous import job_id
            @param commit_job: commit the job to the database
            @param delete_job: delete the import job from the queue
            @param strategy: tuple of allowed import methods (create/update/delete)
            @param update_policy: policy for updates (sync)
            @param conflict_policy: policy for conflict resolution (sync)
            @param last_sync: last synchronization datetime (sync)
            @param onconflict: callback hook for conflict resolution (sync)
            @param args: parameters to pass to the transformation stylesheet
        """

        # Check permission for the resource
        has_permission = current.auth.s3_has_permission
        authorised = has_permission("create", self.table) and \
                     has_permission("update", self.table)
        if not authorised:
            raise IOError("Insufficient permissions")

        xml = current.xml
        tree = None
        self.job = None

        if not job_id:

            # Resource data
            prefix = self.prefix
            name = self.name

            # Additional stylesheet parameters
            tfmt = xml.ISOFORMAT
            utcnow = datetime.datetime.utcnow().strftime(tfmt)
            domain = xml.domain
            base_url = current.response.s3.base_url
            args.update(domain=domain,
                        base_url=base_url,
                        prefix=prefix,
                        name=name,
                        utcnow=utcnow)

            # Build import tree
            if not isinstance(source, (list, tuple)):
                source = [source]
            for item in source:
                if isinstance(item, (list, tuple)):
                    resourcename, s = item[:2]
                else:
                    resourcename, s = None, item
                if isinstance(s, etree._ElementTree):
                    t = s
                elif format == "json":
                    if isinstance(s, basestring):
                        source = StringIO(s)
                        t = xml.json2tree(s)
                    else:
                        t = xml.json2tree(s)
                elif format == "csv":
                    t = xml.csv2tree(s,
                                     resourcename=resourcename,
                                     extra_data=extra_data)
                elif format == "xls":
                    t = xml.xls2tree(s,
                                     resourcename=resourcename,
                                     extra_data=extra_data)
                else:
                    t = xml.parse(s)
                if not t:
                    if xml.error:
                        raise SyntaxError(xml.error)
                    else:
                        raise SyntaxError("Invalid source")

                if stylesheet is not None:
                    t = xml.transform(t, stylesheet, **args)
                    _debug(t)
                    if not t:
                        raise SyntaxError(xml.error)

                if not tree:
                    tree = t.getroot()
                else:
                    tree.extend(list(t.getroot()))

            if files is not None and isinstance(files, dict):
                self.files = Storage(files)

        else:
            # job ID given
            pass
        
        response = current.response
        # Flag to let onvalidation/onaccept know this is coming from a Bulk Import
        response.s3.bulk = True
        success = self.import_tree(id, tree,
                                   ignore_errors=ignore_errors,
                                   job_id=job_id,
                                   commit_job=commit_job,
                                   delete_job=delete_job,
                                   strategy=strategy,
                                   update_policy=update_policy,
                                   conflict_policy=conflict_policy,
                                   last_sync=last_sync,
                                   onconflict=onconflict)
        response.s3.bulk = False

        self.files = Storage()

        # Response message
        if format == "json":
            # Whilst all Responses are JSON, it's easier to debug by having the
            # response appear in the browser than launching a text editor
            response.headers["Content-Type"] = "application/json"
        if self.error_tree is not None:
            tree = xml.tree2json(self.error_tree)
        else:
            tree = None

        import_info = {"records":self.import_count}
        created = self.import_created
        if created:
            import_info["created"] = created
        updated = self.import_updated
        if updated:
            import_info["updated"] = updated
        deleted = self.import_deleted
        if deleted:
            import_info["deleted"] = deleted

        if success is True:
            return xml.json_message(message=self.error, tree=tree,
                                    **import_info)
        elif success and hasattr(success, "job_id"):
            self.job = success
            return xml.json_message(message=self.error, tree=tree,
                                    **import_info)
        else:
            return xml.json_message(False, 400,
                                    message=self.error, tree=tree)

    # -------------------------------------------------------------------------
    def import_tree(self, id, tree,
                    job_id=None,
                    ignore_errors=False,
                    delete_job=False,
                    commit_job=True,
                    strategy=None,
                    update_policy=None,
                    conflict_policy=None,
                    last_sync=None,
                    onconflict=None):
        """
            Import data from an S3XML element tree.

            @param id: record ID or list of record IDs to update
            @param tree: the element tree
            @param ignore_errors: continue at errors (=skip invalid elements)

            @param job_id: restore a job from the job table (ID or UID)
            @param delete_job: delete the import job from the job table
            @param commit_job: commit the job (default)

            @todo: update for link table support
        """

        from s3import import S3ImportJob

        db = current.db
        xml = current.xml
        auth = current.auth
        tablename = self.tablename
        table = self.table

        if job_id is not None:

            # Restore a job from the job table
            self.error = None
            self.error_tree = None
            try:
                import_job = S3ImportJob(table,
                                         job_id=job_id,
                                         strategy=strategy,
                                         update_policy=update_policy,
                                         conflict_policy=conflict_policy,
                                         last_sync=last_sync,
                                         onconflict=onconflict)
            except:
                self.error = current.ERROR.BAD_SOURCE
                return False

            # Delete the job?
            if delete_job:
                import_job.delete()
                return True

            # Load all items
            job_id = import_job.job_id
            item_table = import_job.item_table
            items = db(item_table.job_id == job_id).select()
            load_item = import_job.load_item
            for item in items:
                success = load_item(item)
                if not success:
                    self.error = import_job.error
                    self.error_tree = import_job.error_tree
            import_job.restore_references()

            # this is only relevant for commit_job=True
            if commit_job:
                if self.error and not ignore_errors:
                    return False
            else:
                return import_job

            # Call the import pre-processor to prepare tables
            # and cleanup the tree as necessary
            import_prep = current.response.s3.import_prep
            if import_prep:
                tree = import_job.get_tree()
                callback(import_prep,
                         # takes tuple (resource, tree) as argument
                         (self, tree),
                         tablename=tablename)
                # Skip import?
                if self.skip_import:
                    _debug("Skipping import to %s" % self.tablename)
                    self.skip_import = False
                    return True

        else:
            # Create a new job from an element tree
            # Do not import into tables without "id" field
            if "id" not in table.fields:
                self.error = current.ERROR.BAD_RESOURCE
                return False

            # Reset error and error tree
            self.error = None
            self.error_tree = None

            # Call the import pre-processor to prepare tables
            # and cleanup the tree as necessary
            import_prep = current.response.s3.import_prep
            if import_prep:
                if not isinstance(tree, etree._ElementTree):
                    tree = etree.ElementTree(tree)
                callback(import_prep,
                         # takes tuple (resource, tree) as argument
                         (self, tree),
                         tablename=tablename)
                # Skip import?
                if self.skip_import:
                    _debug("Skipping import to %s" % self.tablename)
                    self.skip_import = False
                    return True

            # Select the elements for this table
            elements = xml.select_resources(tree, tablename)
            if not elements:
                # nothing to import => still ok
                return True

            # Find matching elements, if a target record ID is given
            UID = xml.UID
            if id and UID in table:
                if not isinstance(id, (tuple, list)):
                    query = (table._id == id)
                else:
                    query = (table._id.belongs(id))
                originals = db(query).select(table[UID])
                uids = [row[UID] for row in originals]
                matches = []
                import_uid = xml.import_uid
                append = matches.append
                for element in elements:
                    element_uid = import_uid(element.get(UID, None))
                    if not element_uid:
                        continue
                    if element_uid in uids:
                        append(element)
                if not matches:
                    first = elements[0]
                    if len(elements) and not first.get(UID, None):
                        first.set(UID, uids[0])
                        matches = [first]
                if not matches:
                    self.error = current.ERROR.NO_MATCH
                    return False
                else:
                    elements = matches

            # Import all matching elements
            import_job = S3ImportJob(table,
                                     tree=tree,
                                     files=self.files,
                                     strategy=strategy,
                                     update_policy=update_policy,
                                     conflict_policy=conflict_policy,
                                     last_sync=last_sync,
                                     onconflict=onconflict)
            add_item = import_job.add_item
            for element in elements:
                success = add_item(element=element,
                                   components=self.components)
                if not success:
                    self.error = import_job.error
                    self.error_tree = import_job.error_tree
            if self.error and not ignore_errors:
                return False

        # Commit the import job
        auth.rollback = not commit_job
        success = import_job.commit(ignore_errors=ignore_errors,
                                    log_items = self.get_config("oncommit_import_item"))
        auth.rollback = False
        self.error = import_job.error
        self.import_count += import_job.count
        self.import_created += import_job.created
        self.import_updated += import_job.updated
        self.import_deleted += import_job.deleted
        job_mtime = import_job.mtime
        if self.mtime is None or \
           job_mtime and job_mtime > self.mtime:
            self.mtime = job_mtime
        if self.error:
            if ignore_errors:
                self.error = "%s - invalid items ignored" % self.error
            self.error_tree = import_job.error_tree
        elif not success:
            # Oops - how could this happen? We can have an error
            # without failure, but not a failure without error!
            # If we ever get here, then there's a bug without a
            # chance to recover - hence let it crash:
            raise RuntimeError("Import failed without error message")
        if not success or not commit_job:
            db.rollback()
        if not commit_job:
            import_job.store()
            return import_job
        else:
            # Remove the job when committed
            if job_id is not None:
                import_job.delete()

        return self.error is None or ignore_errors

    # -------------------------------------------------------------------------
    # XML introspection
    # -------------------------------------------------------------------------
    def export_options(self,
                       component=None,
                       fields=None,
                       only_last=False,
                       show_uids=False,
                       as_json=False):
        """
            Export field options of this resource as element tree

            @param component: name of the component which the options are
                requested of, None for the primary table
            @param fields: list of names of fields for which the options
                are requested, None for all fields (which have options)
            @param as_json: convert the output into JSON
            @param only_last: Obtain the latest record (performance bug fix,
                timeout at s3_tb_refresh for non-dropdown form fields)
        """

        if component is not None:
            c = self.components.get(component, None)
            if c:
                tree = c.export_options(fields=fields,
                                        only_last=only_last,
                                        show_uids=show_uids,
                                        as_json=as_json)
                return tree
            else:
                raise AttributeError
        else:
            if as_json and only_last and len(fields) == 1:
                db = current.db
                component_tablename = "%s_%s" % (self.prefix, self.name)
                field = db[component_tablename][fields[0]]
                req = field.requires
                if isinstance(req, IS_EMPTY_OR):
                    req = req.other
                from s3validators import IS_LOCATION
                if not isinstance(req, (IS_ONE_OF, IS_LOCATION)):
                    raise RuntimeError, "not isinstance(req, IS_ONE_OF)"
                kfield = db[req.ktable][req.kfield]
                rows = db().select(kfield,
                                   orderby=~kfield,
                                   limitby=(0, 1))
                res = []
                for row in rows:
                    val = row[req.kfield]
                    if field.represent:
                        represent = field.represent(val)
                    else:
                        represent = s3_unicode(val)
                    if isinstance(represent, A):
                        represent = represent.components[0]
                    res.append({"@value": val, "$": represent})
                return json.dumps({'option': res})

            xml = current.xml
            tree = xml.get_options(self.prefix,
                                   self.name,
                                   show_uids=show_uids,
                                   fields=fields)

            if as_json:
                return xml.tree2json(tree, pretty_print=False,
                                     native=True)
            else:
                return xml.tostring(tree, pretty_print=False)

    # -------------------------------------------------------------------------
    def export_fields(self, component=None, as_json=False):
        """
            Export a list of fields in the resource as element tree

            @param component: name of the component to lookup the fields
                              (None for primary table)
            @param as_json: convert the output XML into JSON
        """

        if component is not None:
            c = self.components.get(component, None)
            if c:
                tree = c.export_fields()
                return tree
            else:
                raise AttributeError
        else:
            xml = current.xml
            tree = xml.get_fields(self.prefix, self.name)
            if as_json:
                return xml.tree2json(tree, pretty_print=True)
            else:
                return xml.tostring(tree, pretty_print=True)

    # -------------------------------------------------------------------------
    def export_struct(self,
                      meta=False,
                      options=False,
                      references=False,
                      stylesheet=None,
                      as_json=False,
                      as_tree=False):
        """
            Get the structure of the resource

            @param options: include option lists in option fields
            @param references: include option lists even for reference fields
            @param stylesheet: the stylesheet to use for transformation
            @param as_json: convert into JSON after transformation
        """

        xml = current.xml

        # Get the structure of the main resource
        root = etree.Element(xml.TAG.root)
        main = xml.get_struct(self.prefix, self.name,
                              alias=self.alias,
                              parent=root,
                              meta=meta,
                              options=options,
                              references=references)

        # Include the selected components
        for component in self.components.values():
            prefix = component.prefix
            name = component.name
            sub = xml.get_struct(prefix, name,
                                 alias=component.alias,
                                 parent=main,
                                 meta=meta,
                                 options=options,
                                 references=references)

        # Transformation
        tree = etree.ElementTree(root)
        if stylesheet is not None:
            tfmt = xml.ISOFORMAT
            args = dict(domain=xml.domain,
                        base_url=current.response.s3.base_url,
                        prefix=self.prefix,
                        name=self.name,
                        utcnow=datetime.datetime.utcnow().strftime(tfmt))

            tree = xml.transform(tree, stylesheet, **args)
            if tree is None:
                return None

        # Return tree if requested
        if as_tree:
            return tree

        # Otherwise string-ify it
        if as_json:
            return xml.tree2json(tree, pretty_print=True)
        else:
            return xml.tostring(tree, pretty_print=True)

    # -------------------------------------------------------------------------
    # Data Model Helpers
    # -------------------------------------------------------------------------
    @classmethod
    def original(cls, table, record, mandatory=None):
        """
            Find the original record for a possible duplicate:
                - if the record contains a UUID, then only that UUID is used
                  to match the record with an existing DB record
                - otherwise, if the record contains some values for unique
                  fields, all of them must match the same existing DB record

            @param table: the table
            @param record: the record as dict or S3XML Element
        """

        db = current.db
        xml = current.xml
        xml_decode = xml.xml_decode

        VALUE = xml.ATTRIBUTE["value"]
        UID = xml.UID
        ATTRIBUTES_TO_FIELDS = xml.ATTRIBUTES_TO_FIELDS

        # Get primary keys
        pkeys = [f for f in table.fields if table[f].unique]
        pvalues = Storage()

        # Get the values from record
        get = record.get
        if type(record) is etree._Element: #isinstance(record, etree._Element):
            xpath = record.xpath
            xexpr = "%s[@%s='%%s']" % (xml.TAG["data"],
                                       xml.ATTRIBUTE["field"])
            for f in pkeys:
                v = None
                if f == UID or f in ATTRIBUTES_TO_FIELDS:
                    v = get(f, None)
                else:
                    child = xpath(xexpr % f)
                    if child:
                        child = child[0]
                        v = child.get(VALUE, xml_decode(child.text))
                if v:
                    pvalues[f] = v
        elif isinstance(record, dict):
            for f in pkeys:
                v = get(f, None)
                if v:
                    pvalues[f] = v
        else:
            raise TypeError

        # Build match query
        query = None
        for f in pvalues:
            if f == UID:
                continue
            _query = (table[f] == pvalues[f])
            if query is not None:
                query = query | _query
            else:
                query = _query

        fields = cls.import_fields(table, pvalues, mandatory=mandatory)

        # Try to find exactly one match by non-UID unique keys
        if query is not None:
            original = db(query).select(limitby=(0, 2), *fields)
            if len(original) == 1:
                return original.first()

        # If no match, then try to find a UID-match
        if UID in pvalues:
            uid = xml.import_uid(pvalues[UID])
            query = (table[UID] == uid)
            original = db(query).select(limitby=(0, 1), *fields).first()
            if original:
                return original

        # No match or multiple matches
        return None

    # -------------------------------------------------------------------------
    @staticmethod
    def import_fields(table, data, mandatory=None):
        
        fnames = set(s3_all_meta_field_names())
        fnames.add(table._id.name)
        if mandatory:
            fnames |= set(mandatory)
        for fn in data:
            fnames.add(fn)
        return [table[fn] for fn in fnames if fn in table.fields]

    # -------------------------------------------------------------------------
    def readable_fields(self, subset=None):
        """
            Get a list of all readable fields in the resource table

            @param subset: list of fieldnames to limit the selection to
        """

        fkey = None
        table = self.table

        if self.parent and self.linked is None:
            component = self.parent.components.get(self.alias, None)
            if component:
                fkey = component.fkey
        elif self.linked is not None:
            component = self.linked
            if component:
                fkey = component.lkey

        if subset:
            return [ogetattr(table, f) for f in subset
                    if f in table.fields and \
                       ogetattr(table, f).readable and f != fkey]
        else:
            return [ogetattr(table, f) for f in table.fields
                    if ogetattr(table, f).readable and f != fkey]

    # -------------------------------------------------------------------------
    def resolve_selectors(self, selectors,
                          skip_components=False,
                          extra_fields=True,
                          show=True):
        """
            Resolve a list of field selectors against this resource

            @param selectors: the field selectors
            @param skip_components: skip fields in components
            @param extra_fields: automatically add extra_fields of all virtual
                                 fields in this table
            @param show: default for S3ResourceField.show

            @return: tuple of (fields, joins, left, distinct)
        """

        prefix = lambda s: "~.%s" % s \
                           if "." not in s.split("$", 1)[0] else s

        # Store field selectors
        display_fields = []
        append = display_fields.append
        for _s in selectors:
            if isinstance(_s, tuple):
                s = _s[-1]
            else:
                s = _s
            if isinstance(s, S3ResourceField):
                selector = s.selector
            elif isinstance(s, FS):
                selector = s.name
            else:
                selector = s
            append(prefix(selector))
        slist = list(selectors)

        # Collect extra fields from virtual tables
        if extra_fields:
            append = slist.append
            extra = self.get_config("extra_fields", [])
            for selector in extra:
                s = prefix(selector)
                if s not in display_fields:
                    append(s)

        joins = Storage()
        left = Storage()

        distinct = False


        rfields = []
        columns = []
        append = rfields.append
        for s in slist:

            # Allow to override the field label
            if isinstance(s, tuple):
                label, selector = s
            else:
                label, selector = None, s

            # Resolve the selector
            if isinstance(selector, str):
                selector = prefix(selector)
                try:
                    rfield = S3ResourceField(self, selector, label=label)
                except (AttributeError, SyntaxError):
                    continue
            elif isinstance(selector, FS):
                try:
                    rfield = selector.resolve(self)
                except (AttributeError, SyntaxError):
                    continue
            elif isinstance(selector, S3ResourceField):
                rfield = selector
            else:
                continue

            # Unresolvable selector?
            if rfield.field is None and not rfield.virtual:
                continue

            # Replace default label
            if label is not None:
                rfield.label = label
                
            # Skip components
            if skip_components:
                head = rfield.selector.split("$", 1)[0]
                if "." in head and head.split(".")[0] not in ("~", self.alias):
                    continue

            # De-duplicate columns
            if rfield.colname in columns:
                continue
            else:
                columns.append(rfield.colname)

            # Resolve the joins
            if rfield.distinct:
                if rfield.left:
                    left.update(rfield.left)
                distinct = True
            elif rfield.join:
                joins.update(rfield.join)

            rfield.show = show and rfield.selector in display_fields
            append(rfield)

        return (rfields, joins, left, distinct)

    # -------------------------------------------------------------------------
    def resolve_selector(self, selector):
        """
            Wrapper for S3ResourceField, retained for backward compatibility
        """

        return S3ResourceField(self, selector)

    # -------------------------------------------------------------------------
    def split_fields(self, skip=[], data=None, references=None):
        """
            Split the readable fields in the resource table into
            reference and non-reference fields.

            @param skip: list of field names to skip
            @param data: data fields to include (None for all)
            @param references: foreign key fields to include (None for all)
        """

        rfields = self.rfields
        dfields = self.dfields

        if rfields is None or dfields is None:
            if self.tablename == "gis_location":
                if "wkt" not in skip:
                    # Skip Bulky WKT fields
                    skip.append("wkt")
                if current.deployment_settings.get_gis_spatialdb() and \
                   "the_geom" not in skip:
                    skip.append("the_geom")

            xml = current.xml
            UID = xml.UID
            IGNORE_FIELDS = xml.IGNORE_FIELDS
            FIELDS_TO_ATTRIBUTES = xml.FIELDS_TO_ATTRIBUTES

            show_ids = current.xml.show_ids
            rfields = []
            dfields = []
            table = self.table
            pkey = table._id.name
            for f in table.fields:
                if f == UID or \
                   f in skip or \
                   f in IGNORE_FIELDS:
                    if f != pkey or not show_ids:
                        continue
                if s3_has_foreign_key(table[f]) and \
                    f not in FIELDS_TO_ATTRIBUTES and \
                    (references is None or f in references):
                    rfields.append(f)
                elif data is None or \
                     f in data or \
                     f in FIELDS_TO_ATTRIBUTES:
                    dfields.append(f)
            self.rfields = rfields
            self.dfields = dfields

        return (rfields, dfields)

    # -------------------------------------------------------------------------
    # Utility functions
    # -------------------------------------------------------------------------
    def configure(self, **settings):
        """
            Update configuration settings for this resource

            @param settings: configuration settings for this resource
                             as keyword arguments
        """

        current.s3db.configure(self.tablename, **settings)

    # -------------------------------------------------------------------------
    def get_config(self, key, default=None):
        """
            Get a configuration setting for the current resource

            @param key: the setting key
            @param default: the default value to return if the setting
                            is not configured for this resource
        """

        return current.s3db.get_config(self.tablename, key, default=default)

    # -------------------------------------------------------------------------
    def limitby(self, start=0, limit=0):
        """
            Convert start+limit parameters into a limitby tuple
                - limit without start => start = 0
                - start without limit => limit = ROWSPERPAGE
                - limit 0 (or less)   => limit = 1
                - start less than 0   => start = 0

            @param start: index of the first record to select
            @param limit: maximum number of records to select
        """

        if limit is None:
            return None
            
        if start is None:
            start = 0
        if limit == 0:
            limit = current.response.s3.ROWSPERPAGE

        if limit <= 0:
            limit = 1
        if start < 0:
            start = 0

        return (start, start + limit)

    # -------------------------------------------------------------------------
    def get_join(self):
        """ Get join for this component """

        if self.parent is None:
            # This isn't a component
            return None
        else:
            ltable = self.parent.table

        rtable = self.table
        pkey = self.pkey
        fkey = self.fkey

        DELETED = current.xml.DELETED

        if self.linked:
            return self.linked.get_join()

        elif self.linktable:
            linktable = self.linktable
            lkey = self.lkey
            rkey = self.rkey
            join = ((ltable[pkey] == linktable[lkey]) &
                    (linktable[rkey] == rtable[fkey]))
            if DELETED in linktable:
                join = ((linktable[DELETED] != True) & join)

        else:
            join = (ltable[pkey] == rtable[fkey])
            if DELETED in rtable:
                join &= (rtable[DELETED] != True)

        if self.filter is not None:
            join &= self.filter

        return join

    # -------------------------------------------------------------------------
    def get_left_join(self):
        """ Get a left join for this component """

        if self.parent is None:
            # This isn't a component
            return None
        else:
            ltable = self.parent.table

        rtable = self.table
        pkey = self.pkey
        fkey = self.fkey

        DELETED = current.xml.DELETED

        if self.linked:
            return self.linked.get_left_join()

        elif self.linktable:
            linktable = self.linktable
            lkey = self.lkey
            rkey = self.rkey
            lquery = (ltable[pkey] == linktable[lkey])
            if DELETED in linktable:
                lquery &= (linktable[DELETED] != True)

            if self.filter is not None:
                rquery = (linktable[rkey] == rtable[fkey]) & self.filter
            else:    
                rquery = (linktable[rkey] == rtable[fkey])
                
            join = [linktable.on(lquery),
                    rtable.on(rquery)] 

        else:
            lquery = (ltable[pkey] == rtable[fkey])
            if DELETED in rtable:
                lquery &= (rtable[DELETED] != True)

            if self.filter is not None:
                lquery &= self.filter

            join = [rtable.on(lquery)]

        return join

    # -------------------------------------------------------------------------
    def get_reverse_left_join(self):
        """ Get a reverse left join for this component """

        if self.parent is None:
            # This isn't a component
            return None
        else:
            ltable = self.parent.table

        rtable = self.table
        pkey = self.pkey
        fkey = self.fkey

        if self.linked:
            return self.linked.get_left_join()
        elif self.linktable:
            linktable = self.linktable
            lkey = self.lkey
            rkey = self.rkey
            lquery = (linktable[lkey] == ltable[pkey])
            DELETED = current.xml.DELETED
            if DELETED in linktable:
                lquery &= (linktable[DELETED] != True)
            rquery = (rtable[fkey] == linktable[rkey])
            join = [linktable.on(rquery), ltable.on(lquery)]
        else:
            lquery = (rtable[fkey] == ltable[pkey])
            join = [ltable.on(lquery)]

        return join

    # -------------------------------------------------------------------------
    def link_id(self, master_id, component_id):
        """
            Helper method to find the link table entry ID for
            a pair of linked records.

            @param master_id: the ID of the master record
            @param component_id: the ID of the component record
        """

        if self.parent is None or self.linked is None:
            return None

        join = self.get_join()
        ltable = self.table
        mtable = self.parent.table
        ctable = self.linked.table
        query = join & \
                (mtable._id == master_id) & \
                (ctable._id == component_id)
        row = current.db(query).select(ltable._id, limitby=(0, 1)).first()
        if row:
            return row[ltable._id.name]
        else:
            return None

    # -------------------------------------------------------------------------
    def component_id(self, master_id, link_id):
        """
            Helper method to find the component record ID for
            a particular link of a particular master record

            @param link: the link (S3Resource)
            @param master_id: the ID of the master record
            @param link_id: the ID of the link table entry
        """

        if self.parent is None or self.linked is None:
            return None

        join = self.get_join()
        ltable = self.table
        mtable = self.parent.table
        ctable = self.linked.table
        query = join & (ltable._id == link_id)
        if master_id is not None:
            # master ID is redundant, but can be used to check negatives
            query &= (mtable._id == master_id)
        row = current.db(query).select(ctable._id, limitby=(0, 1)).first()
        if row:
            return row[ctable._id.name]
        else:
            return None

    # -------------------------------------------------------------------------
    def update_link(self, master, record):
        """
            Create a new link in a link table if it doesn't yet exist.
            This function is meant to also update links in "embed"
            actuation mode once this gets implemented, therefore the
            method name "update_link".

            @param master: the master record
            @param record: the new component record to be linked
        """

        if self.parent is None or self.linked is None:
            return None

        # Find the keys
        resource = self.linked
        pkey = resource.pkey
        lkey = resource.lkey
        rkey = resource.rkey
        fkey = resource.fkey
        if pkey not in master:
            return None
        _lkey = master[pkey]
        if fkey not in record:
            return None
        _rkey = record[fkey]
        if not _lkey or not _rkey:
            return None

        ltable = self.table
        ltn = ltable._tablename
        s3db = current.s3db
        onaccept = s3db.get_config(ltn, "create_onaccept",
                   s3db.get_config(ltn, "onaccept", None))

        # Create the link if it does not already exist
        query = ((ltable[lkey] == _lkey) &
                 (ltable[rkey] == _rkey))
        row = current.db(query).select(ltable._id, limitby=(0, 1)).first()
        if not row:
            form = Storage(vars=Storage({lkey:_lkey, rkey:_rkey}))
            link_id = ltable.insert(**form.vars)
            if link_id and onaccept:
                form.vars[ltable._id.name] = link_id
                callback(onaccept, form)
        else:
            link_id = row[ltable._id.name]
        return link_id

    # -------------------------------------------------------------------------
    def datatable_filter(self, fields, get_vars):
        """
            Parse datatable search/sort vars into a tuple of
            query, orderby and left joins

            @param fields: list of field selectors representing
                           the order of fields in the datatable (list_fields)
            @param get_vars: the datatable GET vars

            @return: tuple of (query, orderby, left joins)
        """

        db = current.db

        left_joins = S3LeftJoins(self.tablename)

        sSearch = "sSearch"
        iColumns = "iColumns"
        iSortingCols = "iSortingCols"

        parent = self.parent
        fkey = self.fkey

        # Skip joins for linked tables
        if self.linked is not None:
            skip = self.linked.tablename
        else:
            skip = None

        # Resolve the list fields
        rfields = self.resolve_selectors(fields)[0]

        # FILTER --------------------------------------------------------------

        searchq = None
        if sSearch in get_vars and iColumns in get_vars:

            # Build filter
            text = get_vars[sSearch]
            words = [w for w in text.lower().split()]

            if words:
                try:
                    numcols = int(get_vars[iColumns])
                except ValueError:
                    numcols = 0

                flist = []
                for i in xrange(numcols):
                    try:
                        rfield = rfields[i]
                        field = rfield.field
                    except (KeyError, IndexError):
                        continue
                    if field is None:
                        continue
                    ftype = str(field.type)

                    # Add left joins
                    left_joins.extend(rfield.left)

                    if ftype[:9] == "reference" and \
                       hasattr(field, "sortby") and field.sortby:
                        # For foreign keys, we search through their sortby

                        # Get the lookup table
                        tn = ftype[10:]
                        if parent is not None and \
                           parent.tablename == tn and field.name != fkey:
                            alias = "%s_%s_%s" % (parent.prefix,
                                                  "linked",
                                                  parent.name)
                            ktable = db[tn].with_alias(alias)
                            ktable._id = ktable[ktable._id.name]
                            tn = alias
                        elif tn == field.tablename:
                            prefix, name = field.tablename.split("_", 1)
                            alias = "%s_%s_%s" % (prefix, field.name, name)
                            ktable = db[tn].with_alias(alias)
                            ktable._id = ktable[ktable._id.name]
                            tn = alias
                        else:
                            ktable = db[tn]

                        # Add left join for lookup table
                        if tn != skip:
                            left_joins.add(ktable.on(field == ktable._id))

                        if isinstance(field.sortby, (list, tuple)):
                            flist.extend([ktable[f] for f in field.sortby
                                                    if f in ktable.fields])
                        else:
                            if field.sortby in ktable.fields:
                                flist.append(ktable[field.sortby])

                    else:
                        # Otherwise, we search through the field itself
                        flist.append(field)
                        
            # Build search query
            # @todo: migrate this to S3ResourceQuery?
            opts = Storage()
            queries = []
            for w in words:

                wqueries = []
                for field in flist:
                    ftype = str(field.type)
                    options = None
                    fname = str(field)
                    if fname in opts:
                        options = opts[fname]
                    elif ftype[:7] in ("integer",
                                       "list:in",
                                       "list:st",
                                       "referen",
                                       "list:re",
                                       "string"):
                        requires = field.requires
                        if not isinstance(requires, (list, tuple)):
                            requires = [requires]
                        if requires:
                            r = requires[0]
                            if isinstance(r, IS_EMPTY_OR):
                                r = r.other
                            if hasattr(r, "options"):
                                try:
                                    options = r.options()
                                except:
                                    options = []
                    if options is None and ftype in ("string", "text"):
                        wqueries.append(field.lower().like("%%%s%%" % w))
                    elif options is not None:
                        opts[fname] = options
                        vlist = [v for v, t in options
                                   if s3_unicode(t).lower().find(s3_unicode(w)) != -1]
                        if vlist:
                            wqueries.append(field.belongs(vlist))
                if len(wqueries):
                    queries.append(reduce(lambda x, y: x | y \
                                                 if x is not None else y,
                                          wqueries))
            if len(queries):
                searchq = reduce(lambda x, y: x & y \
                                        if x is not None else y, queries)

        # ORDERBY -------------------------------------------------------------

        orderby = []
        if iSortingCols in get_vars:

            # Sorting direction
            def direction(i):
                sort_dir = get_vars["sSortDir_%s" % str(i)]
                return sort_dir and " %s" % sort_dir or ""

            # Get the fields to order by
            try:
                numcols = int(get_vars[iSortingCols])
            except:
                numcols = 0
            columns = []
            for i in xrange(numcols):
                try:
                    iSortCol = int(get_vars["iSortCol_%s" % i])
                    # Map sortable-column index to the real list_fields
                    # index: for every non-sortable column to the left
                    # of sortable column subtract 1
                    for j in xrange(iSortCol):
                        if get_vars.get("bSortable_%s" % j, "true") == "false":
                            iSortCol -= 1
                    rfield = rfields[iSortCol + 1]
                except:
                    # iSortCol_x is either not present in vars or specifies
                    # a non-existent column (i.e. iSortCol_x >= numcols) =>
                    # ignore silently
                    columns.append(Storage(field=None))
                else:
                    columns.append(rfield)

            # Process the orderby-fields
            for i in xrange(len(columns)):
                rfield = columns[i]
                field = rfield.field
                if field is None:
                    continue
                ftype = str(field.type)

                represent = field.represent
                if not hasattr(represent, "skip_dt_orderby") and \
                   hasattr(represent, "dt_orderby"):
                    # Custom orderby logic in field.represent
                    field.represent.dt_orderby(field,
                                               direction(i),
                                               orderby,
                                               left_joins)

                elif ftype[:9] == "reference" and \
                   hasattr(field, "sortby") and field.sortby:
                    # Foreign keys with sortby will be sorted by sortby
                    
                    # Get the lookup table
                    tn = ftype[10:]
                    if parent is not None and \
                       parent.tablename == tn and field.name != fkey:
                        alias = "%s_%s_%s" % (parent.prefix, "linked", parent.name)
                        ktable = db[tn].with_alias(alias)
                        ktable._id = ktable[ktable._id.name]
                        tn = alias
                    elif tn == field.tablename:
                        prefix, name = field.tablename.split("_", 1)
                        alias = "%s_%s_%s" % (prefix, field.name, name)
                        ktable = db[tn].with_alias(alias)
                        ktable._id = ktable[ktable._id.name]
                        tn = alias
                    else:
                        ktable = db[tn]

                    # Add left joins for lookup table
                    if tn != skip:
                        left_joins.extend(rfield.left)
                        left_joins.add(ktable.on(field == ktable._id))

                    # Construct orderby from sortby
                    if not isinstance(field.sortby, (list, tuple)):
                        orderby.append("%s.%s%s" % (tn, field.sortby, direction(i)))
                    else:
                        orderby.append(", ".join(["%s.%s%s" %
                                                  (tn, fn, direction(i))
                                                  for fn in field.sortby]))

                else:
                    # Otherwise, we sort by the field itself
                    orderby.append("%s%s" % (field, direction(i)))

        if orderby:
            orderby = ", ".join(orderby)
        else:
            orderby = None

        left_joins = left_joins.as_list(tablenames=left_joins.joins.keys())
        return (searchq, orderby, left_joins)

    # -------------------------------------------------------------------------
    def axisfilter(self, axes):
        """
            Get all values for the given S3ResourceFields (axes) which
            match the resource query, used in pivot tables to filter out
            additional values where dimensions can have multiple values
            per record

            @param axes: the axis fields as list/tuple of S3ResourceFields

            @return: a dict with values per axis, only containes those
                     axes which are affected by the resource filter
        """

        axisfilter = {}

        qdict = self.get_query().as_dict(flat=True)

        for rfield in axes:
            field = rfield.field

            if field is None:
                # virtual field or unresolvable selector
                continue

            left_joins = S3LeftJoins(self.tablename)
            left_joins.extend(rfield.left)

            tablenames = left_joins.joins.keys()
            tablenames.append(self.tablename)
            af = S3AxisFilter(qdict, tablenames)

            if af.op is not None:
                query = af.query()
                left = left_joins.as_list()

                # @todo: this does not work with virtual fields: need
                # to retrieve all extra_fields for the dimension table
                # and can't groupby (=must deduplicate afterwards)
                rows = current.db(query).select(field,
                                                left=left,
                                                groupby=field)
                colname = rfield.colname
                if rfield.ftype[:5] == "list:":
                    values = []
                    vappend = values.append
                    for row in rows:
                        v = row[colname]
                        if v:
                            vappend(v)
                    values = set(chain.from_iterable(values))
                    
                    include, exclude = af.values(rfield)
                    fdict = {}
                    if include:
                        for v in values:
                            vstr = s3_unicode(v)
                            if vstr in include and vstr not in exclude:
                                fdict[v] = None
                    else:
                        fdict = dict((v, None) for v in values)
                        
                    axisfilter[colname] = fdict
                    
                else:
                    axisfilter[colname] = dict((row[colname], None)
                                               for row in rows)

        return axisfilter

    # -------------------------------------------------------------------------
    def prefix_selector(self, selector):
        """
            Helper method to ensure consistent prefixing of field selectors

            @param selector: the selector
        """

        head = selector.split("$", 1)[0]
        if "." in head:
            prefix = head.split(".", 1)[0]
            if prefix == self.alias:
                return selector.replace("%s." % prefix, "~.")
            else:
                return selector
        else:
            return "~.%s" % selector

    # -------------------------------------------------------------------------
    def list_fields(self, key="list_fields", id_column=0):
        """
            Get the list_fields for this resource

            @param key: alternative key for the table configuration
            @param id_column: True/False, whether to include the record ID
                              or not, or 0 to enforce the record ID to be
                              the first column
        """

        list_fields = self.get_config(key, None)
        if not list_fields and key != "list_fields":
            list_fields = self.get_config("list_fields", None)
        if not list_fields:
            list_fields = [f.name for f in self.readable_fields()]

        pkey = _pkey = self._id.name
        fields = []
        append = fields.append
        selectors = set()
        seen = selectors.add
        for f in list_fields:
            selector = f if type(f) is not tuple else f[1]
            if selector == _pkey and not id_column:
                pkey = f
            elif selector not in selectors:
                seen(selector)
                append(f)
        if id_column is 0:
            fields.insert(0, pkey)
        return fields
        
    # -------------------------------------------------------------------------
    @property
    def _table(self):
        """
            Get the original Table object (without SQL Alias), this
            is required for SQL update (DAL doesn't detect the alias
            and uses the wrong tablename).
        """

        if self.tablename != self._alias:
            return current.s3db[self.tablename]
        else:
            return self.table

# =============================================================================
class S3AxisFilter(object):
    """
        Experimental: helper class to extract filter values for pivot
        table axis fields
    """

    # -------------------------------------------------------------------------
    def __init__(self, qdict, tablenames):
        """
            Constructor, recursively introspect the query dict and extract
            all relevant subqueries.

            @param qdict: the query dict (from Query.as_dict(flat=True))
            @param tablenames: the names of the relevant tables
        """

        self.l = None
        self.r = None
        self.op = None

        self.tablename = None
        self.fieldname = None

        if not qdict:
            return

        l = qdict["first"]
        if "second" in qdict:
            r = qdict["second"]
        else:
            r = None

        op = qdict["op"]
        
        if "tablename" in l:
            if l["tablename"] in tablenames:
                self.tablename = l["tablename"]
                self.fieldname = l["fieldname"]
                if isinstance(r, dict):
                    self.op = None
                else:
                    self.op = op
                    self.r = r

        elif op == "AND":
            self.l = S3AxisFilter(l, tablenames)
            self.r = S3AxisFilter(r, tablenames)
            if self.l.op or self.r.op:
                self.op = op

        elif op == "OR":
            self.l = S3AxisFilter(l, tablenames)
            self.r = S3AxisFilter(r, tablenames)
            if self.l.op and self.r.op:
                self.op = op

        elif op == "NOT":
            self.l = S3AxisFilter(l, tablenames)
            self.op = op

        else:
            self.l = S3AxisFilter(l, tablenames)
            if self.l.op:
                self.op = op

    # -------------------------------------------------------------------------
    def query(self):
        """ Reconstruct the query from this filter """

        op = self.op
        if op is None:
            return None

        if self.tablename and self.fieldname:
            l = current.s3db[self.tablename][self.fieldname]
        elif self.l:
            l = self.l.query()
        else:
            l = None

        r = self.r
        if op in ("AND", "OR", "NOT"):
            r = r.query() if r else True

        if op == "AND":
            if l is not None and r is not None:
                return l & r
            elif r is not None:
                return r
            else:
                return l
        elif op == "OR":
            if l is not None and r is not None:
                return l | r
            else:
                return None
        elif op == "NOT":
            if l is not None:
                return ~l
            else:
                return None
        elif l is None:
            return None

        if isinstance(r, S3AxisFilter):
            r = r.query()
        if r is None:
            return None

        if op == "LOWER":
            return l.lower()
        elif op == "UPPER":
            return l.upper()
        elif op == "EQ":
            return l == r
        elif op == "NE":
            return l != r
        elif op == "LT":
            return l < r
        elif op == "LE":
            return l <= r
        elif op == "GE":
            return l >= r
        elif op == "GT":
            return l > r
        elif op == "BELONGS":
            return l.belongs(r)
        elif op == "CONTAINS":
            return l.contains(r)
        else:
            return None

    # -------------------------------------------------------------------------
    def values(self, rfield):
        """
            Helper method to filter list:type axis values

            @param rfield: the axis field

            @return: pair of value lists [include], [exclude]
        """

        op = self.op
        tablename = self.tablename
        fieldname = self.fieldname

        if tablename == rfield.tname and \
           fieldname == rfield.fname:
            value = self.r
            if isinstance(value, (list, tuple)):
                value = [s3_unicode(v) for v in value]
            else:
                value = [s3_unicode(value)]
            if op == "CONTAINS":
                return value, []
            elif op == "EQ":
                return value, []
            elif op == "NE":
                return [], value
        elif op == "AND":
            li, le = self.l.values(rfield)
            ri, re = self.r.values(rfield)
            return [v for v in li + ri if v not in le + re], []
        elif op == "OR":
            li, le = self.l.values(rfield)
            ri, re = self.r.values(rfield)
            return [v for v in li + ri], []
        if op == "NOT":
            li, le = self.l.values(rfield)
            return [], li
        return [], []
        
# =============================================================================
class S3ResourceFilter(object):
    """ Class representing a resource filter """

    def __init__(self,
                 resource,
                 id=None,
                 uid=None,
                 filter=None,
                 vars=None,
                 filter_component=None):
        """
            Constructor

            @param resource: the S3Resource
            @param id: the record ID (or list of record IDs)
            @param uid: the record UID (or list of record UIDs)
            @param filter: a filter query (Query or S3ResourceQuery)
            @param vars: the dict of GET vars (URL filters)
            @param filter_component: the alias of the component the URL
                                     filters apply for (filters for this
                                     component must be handled separately)
        """

        self.resource = resource

        # Init
        self.queries = []
        self.filters = []
        self.cqueries = Storage()
        self.cfilters = Storage()
        
        self.query = None
        self.rfltr = None
        self.vfltr = None
        
        self.transformed = None
        
        self.multiple = True
        self.distinct = False

        self.joins = Storage()

        table = resource.table

        # Accessible/available query
        if resource.accessible_query is not None:
            method = []
            if resource._approved:
                method.append("read")
            if resource._unapproved:
                method.append("review")
            mquery = resource.accessible_query(method, table)
        else:
            mquery = (table._id > 0)

        # Deletion status
        DELETED = current.xml.DELETED
        if DELETED in table.fields and not resource.include_deleted:
            remaining = (table[DELETED] != True)
            mquery = remaining & mquery

        # ID query
        if id is not None:
            if not isinstance(id, (list, tuple)):
                self.multiple = False
                mquery = mquery & (table._id == id)
            else:
                mquery = mquery & (table._id.belongs(id))

        # UID query
        UID = current.xml.UID
        if uid is not None and UID in table:
            if not isinstance(uid, (list, tuple)):
                self.multiple = False
                mquery = mquery & (table[UID] == uid)
            else:
                mquery = mquery & (table[UID].belongs(uid))

        parent = resource.parent
        if not parent:
            # Standard master query
            self.mquery = mquery

            # URL queries
            if vars:
                resource.vars = Storage(vars)

                # BBox
                bbox = self.parse_bbox_query(resource, vars)
                if bbox is not None:
                    self.queries.append(bbox)

                # Filters
                add_filter = self.add_filter
                
                # Current concept:
                # Interpret all URL filters in the context of master
                queries = S3URLQuery.parse(resource, vars)
                
                # @todo: Alternative concept (inconsistent?):
                # Interpret all URL filters in the context of filter_component:
                #if filter_component and \
                   #filter_component in resource.components:
                    #context = resource.components[filter_component]
                #else:
                    #context = resource
                #queries = S3URLQuery.parse(context, vars)

                for alias in queries:
                    if filter_component == alias:
                        for q in queries[alias]:
                            add_filter(q, component=alias, master=False)
                    else:
                        for q in queries[alias]:
                            add_filter(q)
                self.cfilters = queries
        else:
            # Parent filter
            pf = parent.rfilter
            if not pf:
                pf = parent.build_query()
                
            # Extended master query
            self.mquery = mquery & pf.get_query() & resource.get_join()

            # Component/link-table specific filters
            add_filter = self.add_filter
            aliases = [resource.alias]
            if resource.link is not None:
                aliases.append(resource.link.alias)
            elif resource.linked is not None:
                aliases.append(resource.linked.alias)
            for alias in aliases:
                for filter_set in (pf.cqueries, pf.cfilters):
                    if alias in filter_set:
                        [add_filter(q) for q in filter_set[alias]]

        # Additional filters
        if filter is not None:
            self.add_filter(filter)

    # -------------------------------------------------------------------------
    def add_filter(self, query, component=None, master=True):
        """
            Extend this filter

            @param query: a Query or S3ResourceQuery object
            @param component: alias of the component the filter shall be
                              added to (None for master)
            @param master: False to filter only component
        """

        alias = None
        if not master:
            if not component:
                return
            if component != self.resource.alias:
                alias = component

        if isinstance(query, S3ResourceQuery):
            self.transformed = None
            filters = self.filters
            cfilters = self.cfilters

            joins, distinct = query.joins(self.resource)
            for tn in joins:
                join = joins[tn]
                if alias not in self.joins:
                    self.joins[alias] = Storage()
                self.joins[alias][tn] = join
                self.add_filter(join, component=component, master=master)
            self.distinct |= distinct

        else:
            # DAL Query
            filters = self.queries
            cfilters = self.cqueries

        self.query = None
        if alias:
            if alias in self.cfilters:
                cfilters[alias].append(query)
            else:
                cfilters[alias] = [query]
        else:
            filters.append(query)
        return

    # -------------------------------------------------------------------------
    def get_query(self):
        """ Get the effective DAL query """

        if self.query is not None:
            return self.query
            
        resource = self.resource
        
        query = reduce(lambda x, y: x & y, self.queries, self.mquery)
        if self.filters:
            if self.transformed is None:

                # Combine all filters
                filters = reduce(lambda x, y: x & y, self.filters)

                # Transform with external search engine
                transformed = filters.transform(resource)
                self.transformed = transformed

                # Split DAL and virtual filters
                self.rfltr, self.vfltr = transformed.split(resource)
                
            if self.rfltr:
                # Add to query
                query &= self.rfltr.query(self.resource)

        self.query = query
        return query

    # -------------------------------------------------------------------------
    def get_filter(self):
        """ Get the effective virtual filter """

        if self.query is None:
            self.get_query()
        return self.vfltr

    # -------------------------------------------------------------------------
    def get_left_joins(self, as_list=True):
        """
            Get all left joins for this filter

            @param as_list: return a flat list rather than a nested dict
        """

        if self.query is None:
            self.get_query()

        left = Storage()
        resource = self.resource
        for q in self.filters:
            joins, distinct = q.joins(resource, left=True)
            left.update(joins)

        # Add cross-component joins if required
        parent = resource.parent
        if parent:
            pf = parent.rfilter
            if pf is None:
                pf = parent.build_query()
            parent_left = pf.get_left_joins(as_list=False)
            tablename = resource._alias
            if parent_left:
                for tn in parent_left:
                    if tn not in left and tn != tablename:
                        left[tn] = parent_left[tn]
                left[parent.tablename] = resource.get_reverse_left_join()

        if as_list:
            return [j for tablename in left for j in left[tablename]]
        else:
            return left

    # -------------------------------------------------------------------------
    def get_fields(self):
        """ Get all field selectors in this filter """

        if self.query is None:
            self.get_query()
            
        if self.vfltr:
            return self.vfltr.fields()
        else:
            return []

    # -------------------------------------------------------------------------
    @staticmethod
    def parse_bbox_query(resource, get_vars):
        """
            Generate a Query from a URL boundary box query; supports multiple
            bboxes, but optimised for the usual case of just 1

            @param resource: the resource
            @param get_vars: the URL GET vars
        """

        tablenames = ("gis_location",
                      "gis_feature_query",
                      "gis_layer_shapefile")

        POLYGON = "POLYGON((%s %s, %s %s, %s %s, %s %s, %s %s))"

        bbox_query = None
        if get_vars:
            for k, v in get_vars.items():
                if k[:4] == "bbox":
                    table = resource.table
                    tablename = resource.tablename
                    fields = table.fields

                    fname = None
                    sname = None
                    if k.find(".") != -1:
                        fname = k.split(".")[1]
                        if fname not in fields:
                            # Field not found - ignore
                            continue
                    elif tablename not in tablenames:
                        for f in fields:
                            if not fname and str(table[f].type) == "reference gis_location":
                                fname = f
                                break
                            if not sname and str(table[f].type) == "reference org_site":
                                sname = f
                    try:
                        minLon, minLat, maxLon, maxLat = v.split(",")
                    except:
                        # Badly-formed bbox - ignore
                        continue
                    else:
                        bbox_filter = None
                        if tablename in ("gis_location", "gis_feature_query"):
                            gtable = table
                        elif tablename == "gis_layer_shapefile":
                            gtable = resource.components.items()[0][1].table
                        else:
                            gtable = current.s3db.gis_location
                            if current.deployment_settings.get_gis_spatialdb():
                                # Use the Spatial Database
                                minLon = float(minLon)
                                maxLon = float(maxLon)
                                minLat = float(minLat)
                                maxLat = float(maxLat)
                                bbox = POLYGON % (minLon, minLat,
                                                  minLon, maxLat,
                                                  maxLon, maxLat,
                                                  maxLon, minLat,
                                                  minLon, minLat)
                                try:
                                    # Spatial DAL & Database
                                    bbox_filter = gtable.the_geom \
                                                        .st_intersects(bbox)
                                except:
                                    # Old DAL or non-spatial database
                                    pass
                        if not bbox_filter:
                            bbox_filter = (gtable.lon > float(minLon)) & \
                                          (gtable.lon < float(maxLon)) & \
                                          (gtable.lat > float(minLat)) & \
                                          (gtable.lat < float(maxLat))
                        if fname is not None:
                            # Need a join
                            join = (gtable.id == table[fname])
                            bbox = (join & bbox_filter)
                        elif sname is not None:
                            # Need a double join
                            stable = current.s3db.org_site
                            join = (stable.site_id == table[sname]) & \
                                   (gtable.id == stable.location_id)
                            bbox = (join & bbox_filter)
                        else:
                            bbox = bbox_filter
                    if bbox_query is None:
                        bbox_query = bbox
                    else:
                        # Merge with the previous BBOX
                        bbox_query = bbox_query & bbox
        return bbox_query

    # -------------------------------------------------------------------------
    def __call__(self, rows, start=None, limit=None):
        """
            Filter a set of rows by the effective virtual filter

            @param rows: a Rows object
            @param start: index of the first matching record to select
            @param limit: maximum number of records to select
        """

        vfltr = self.get_filter()
        
        if rows is None or vfltr is None:
            return rows
        resource = self.resource
        if start is None:
            start = 0
        first = start
        if limit is not None:
            last = start + limit
            if last < first:
                first, last = last, first
            if first < 0:
                first = 0
            if last < 0:
                last = 0
        else:
            last = None
        i = 0
        result = []
        append = result.append
        for row in rows:
            if last is not None and i >= last:
                break
            success = vfltr(resource, row, virtual=True)
            if success or success is None:
                if i >= first:
                    append(row)
                i += 1
        return Rows(rows.db, result,
                    colnames=rows.colnames, compact=False)

    # -------------------------------------------------------------------------
    def count(self, left=None, distinct=False):
        """
            Get the total number of matching records

            @param left: left outer joins
            @param distinct: count only distinct rows
        """

        distinct |= self.distinct

        resource = self.resource
        if resource is None:
            return 0

        table = resource.table

        vfltr = self.get_filter()

        if vfltr is None and not distinct:

            left_joins = S3LeftJoins(resource.tablename, left)
            left_joins.add(self.get_left_joins())
            left = left_joins.as_list()

            cnt = table[table._id.name].count()

            row = current.db(self.query).select(cnt, left=left).first()
            if row:
                return row[cnt]
            else:
                return 0

        else:
            data = resource.select([table._id.name],
                                   # We don't really want to retrieve
                                   # any rows but just count, hence:
                                   limit=1,
                                   count=True)
            return data["numrows"]

    # -------------------------------------------------------------------------
    def __repr__(self):
        """ String representation of the instance """

        resource = self.resource

        left_joins = self.get_left_joins()
        if left_joins:
            left = S3LeftJoins(resource.tablename, left_joins)
            joins = ", ".join([str(j) for j in left.as_list()])
        else:
            left = None
            joins = None

        vfltr = self.get_filter()
        if vfltr:
            vfltr = vfltr.represent(resource)
        else:
            vfltr = None

        represent = "<S3ResourceFilter %s, " \
                    "query=%s, " \
                    "left=[%s], " \
                    "distinct=%s, " \
                    "filter=%s>" % (
                        resource.tablename,
                        self.get_query(),
                        joins,
                        self.distinct,
                        vfltr
                    )

        return represent

    # -------------------------------------------------------------------------
    def serialize_url(self):
        """
            Serialize this filter as URL query

            @return: a Storage of URL GET variables
        """
        
        resource = self.resource
        url_vars = Storage()
        for f in self.filters:
            sub = f.serialize_url(resource=resource)
            url_vars.update(sub)
        return url_vars

# =============================================================================
class S3RecordMerger(object):
    """ Record Merger """

    def __init__(self, resource):
        """
            Constructor

            @param resource: the resource
        """

        self.resource = resource

    # -------------------------------------------------------------------------
    @staticmethod
    def raise_error(msg, error=RuntimeError):
        """
            Roll back the current transaction and raise an error

            @param message: error message
            @param error: exception class to raise
        """

        current.db.rollback()
        raise error(msg)

    # -------------------------------------------------------------------------
    def update_record(self, table, id, row, data):

        form = Storage(vars = Storage([(f, row[f])
                              for f in table.fields if f in row]))
        form.vars.update(data)
        try:
            current.db(table._id==row[table._id]).update(**data)
        except Exception, e:
            self.raise_error("Could not update %s.%s" %
                            (table._tablename, id))
        else:
            s3db = current.s3db
            s3db.update_super(table, form.vars)
            current.auth.s3_set_record_owner(table, row[table._id], force_update=True)
            s3db.onaccept(table, form, method="update")
        return form.vars

    # -------------------------------------------------------------------------
    def delete_record(self, table, id, replaced_by=None):

        s3db = current.s3db

        if replaced_by is not None:
            replaced_by = {str(id): replaced_by}
        resource = s3db.resource(table, id=id)
        success = resource.delete(replaced_by=replaced_by,
                                  cascade=True)
        if not success:
            self.raise_error("Could not delete %s.%s (%s)" %
                            (resource.tablename, id, resource.error))
        return success

    # -------------------------------------------------------------------------
    def merge_realms(self, table, original, duplicate):
        """
            Merge the realms of two person entities (update all
            realm_entities in all records from duplicate to original)

            @param table: the table original and duplicate belong to
            @param original: the original record
            @param duplicate: the duplicate record
        """

        if "pe_id" not in table.fields:
            return

        original_pe_id = original["pe_id"]
        duplicate_pe_id = duplicate["pe_id"]

        db = current.db

        for t in db:
            if "realm_entity" in t.fields:

                query = (t.realm_entity == duplicate_pe_id)
                if "deleted" in t.fields:
                    query &= (t.deleted != True)
                try:
                    db(query).update(realm_entity = original_pe_id)
                except:
                    db.rollback()
                    raise
        return


    # -------------------------------------------------------------------------
    def fieldname(self, key):

        fn = None
        if "." in key:
            alias, fn = key.split(".", 1)
            if alias not in ("~", self.resource.alias):
                fn = None
        elif self.main:
            fn = key
        return fn

    # -------------------------------------------------------------------------
    def merge(self,
              original_id,
              duplicate_id,
              replace=None,
              update=None,
              main=True):
        """
            Merge a duplicate record into its original and remove the
            duplicate, updating all references in the database.

            @param original_id: the ID of the original record
            @param duplicate_id: the ID of the duplicate record
            @param replace: list fields names for which to replace the
                            values in the original record with the values
                            of the duplicate
            @param update: dict of {field:value} to update the final record
            @param main: internal indicator for recursive calls

            @status: work in progress
            @todo: de-duplicate components and link table entries

            @note: virtual references (i.e. non-SQL, without foreign key
                   constraints) must be declared in the table configuration
                   of the referenced table like:

                   s3db.configure(tablename, referenced_by=[(tablename, fieldname)])

                   This does not apply for list:references which will be found
                   automatically.

            @note: this method can only be run from master resources (in order
                   to find all components). To merge component records, you have
                   to re-define the component as a master resource.

            @note: CLI calls must db.commit()
        """

        self.main = main

        db = current.db
        resource = self.resource
        table = resource.table
        tablename = resource.tablename

        # Check for master resource
        if resource.parent:
            self.raise_error("Must not merge from component", SyntaxError)

        # Check permissions
        auth = current.auth
        has_permission = auth.s3_has_permission
        permitted = has_permission("update", table,
                                   record_id = original_id) and \
                    has_permission("delete", table,
                                   record_id = duplicate_id)
        if not permitted:
            self.raise_error("Operation not permitted", auth.permission.error)

        # Load all models
        s3db = current.s3db
        if main:
            s3db.load_all_models()

        # Get the records
        original = None
        duplicate = None
        query = table._id.belongs([original_id, duplicate_id])
        if "deleted" in table.fields:
            query &= (table.deleted != True)
        rows = db(query).select(table.ALL, limitby=(0, 2))
        for row in rows:
            record_id = row[table._id]
            if str(record_id) == str(original_id):
                original = row
                original_id = row[table._id]
            elif str(record_id) == str(duplicate_id):
                duplicate = row
                duplicate_id = row[table._id]
        msg = "Record not found: %s.%s"
        if original is None:
            self.raise_error(msg % (tablename, original_id), KeyError)
        if duplicate is None:
            self.raise_error(msg % (tablename, duplicate_id), KeyError)

        # Find all single-components
        single = Storage()
        for alias in resource.components:
            component = resource.components[alias]
            if not component.multiple:
                single[component.tablename] = component

        # Is this a super-entity?
        is_super_entity = table._id.name != "id" and \
                          "instance_type" in table.fields

        # Find all references
        referenced_by = list(table._referenced_by)

        # Append virtual references
        virtual_references = s3db.get_config(tablename, "referenced_by")
        if virtual_references:
            referenced_by.extend(virtual_references)

        # Find and append list:references
        for t in db:
            for f in t:
                ftype = str(f.type)
                if ftype[:14] == "list:reference" and \
                   ftype[15:15+len(tablename)] == tablename:
                    referenced_by.append((t._tablename, f.name))

        update_record = self.update_record
        delete_record = self.delete_record
        fieldname = self.fieldname

        # Update all references
        define_resource = s3db.resource
        for referee in referenced_by:

            if isinstance(referee, Field):
                tn, fn = referee.tablename, referee.name
            else:
                tn, fn = referee

            se = s3db.get_config(tn, "super_entity")
            if is_super_entity and \
               (isinstance(se, (list, tuple)) and tablename in se or \
                se == tablename):
                # Skip instance types of this super-entity
                continue

            # Reference field must exist
            if tn not in db or fn not in db[tn].fields:
                continue

            rtable = db[tn]
            if tn in single:
                component = single[tn]
                if component.link is not None:
                    component = component.link

                if fn == component.fkey:
                    # Single component => must reduce to one record
                    join = component.get_join()
                    pkey = component.pkey
                    lkey = component.lkey or component.fkey

                    # Get the component records
                    query = (table[pkey] == original[pkey]) & join
                    osub = db(query).select(limitby=(0, 1)).first()
                    query = (table[pkey] == duplicate[pkey]) & join
                    dsub = db(query).select(limitby=(0, 1)).first()

                    ctable = component.table

                    if dsub is None:
                        # No duplicate => skip this step
                        continue
                    elif not osub:
                        # No original => re-link the duplicate
                        dsub_id = dsub[ctable._id]
                        data = {lkey: original[pkey]}
                        success = update_record(ctable, dsub_id, dsub, data)
                    elif component.linked is not None:
                        # Duplicate link => remove it
                        dsub_id = dsub[component.table._id]
                        delete_record(ctable, dsub_id)
                    else:
                        # Two records => merge them
                        osub_id = osub[component.table._id]
                        dsub_id = dsub[component.table._id]
                        cresource = define_resource(component.tablename)
                        cresource.merge(osub_id, dsub_id,
                                        replace=replace,
                                        update=update,
                                        main=False)
                    continue

            # Find the foreign key
            rfield = rtable[fn]
            ktablename, key, multiple = s3_get_foreign_key(rfield)
            if not ktablename:
                if str(rfield.type) == "integer":
                    # Virtual reference
                    key = table._id.name
                else:
                    continue

            # Find the referencing records
            if multiple:
                query = rtable[fn].contains(duplicate[key])
            else:
                query = rtable[fn] == duplicate[key]
            rows = db(query).select(rtable._id, rtable[fn])

            # Update the referencing records
            for row in rows:
                if not multiple:
                    data = {fn:original[key]}
                else:
                    keys = [k for k in row[fn] if k != duplicate[key]]
                    if original[key] not in keys:
                        keys.append(original[key])
                    data = {fn:keys}
                update_record(rtable, row[rtable._id], row, data)

        # Merge super-entity records
        super_entities = resource.get_config("super_entity")
        if super_entities is not None:
            
            if not isinstance(super_entities, (list, tuple)):
                super_entities = [super_entities]
                
            for super_entity in super_entities:
                
                super_table = s3db.table(super_entity)
                if not super_table:
                    continue
                superkey = super_table._id.name
                
                skey_o = original[superkey]
                if not skey_o:
                    msg = "No %s found in %s.%s" % (superkey,
                                                    tablename,
                                                    original_id)
                    current.log.warning(msg)
                    s3db.update_super(table, original)
                    skey_o = original[superkey]
                if not skey_o:
                    continue
                skey_d = duplicate[superkey]
                if not skey_d:
                    msg = "No %s found in %s.%s" % (superkey,
                                                    tablename,
                                                    duplicate_id)
                    current.log.warning(msg)
                    continue

                sresource = define_resource(super_entity)
                sresource.merge(skey_o, skey_d,
                                replace=replace,
                                update=update,
                                main=False)

        # Merge and update original data
        data = Storage()
        if replace:
            for k in replace:
                fn = fieldname(k)
                if fn and fn in duplicate:
                    data[fn] = duplicate[fn]
        if update:
            for k, v in update.items():
                fn = fieldname(k)
                if fn in table.fields:
                    data[fn] = v
        if len(data):
            r = None
            p = Storage([(fn, "__deduplicate_%s__" % fn)
                         for fn in data
                         if table[fn].unique and \
                            table[fn].type == "string" and \
                            data[fn] == duplicate[fn]])
            if p:
                r = Storage([(fn, original[fn]) for fn in p])
                update_record(table, duplicate_id, duplicate, p)
            update_record(table, original_id, original, data)
            if r:
                update_record(table, duplicate_id, duplicate, r)

        # Delete the duplicate
        if not is_super_entity:
            self.merge_realms(table, original, duplicate)
            delete_record(table, duplicate_id, replaced_by=original_id)

        # Success
        return True

# END =========================================================================
