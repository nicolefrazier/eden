# -*- coding: utf-8 -*-

""" Sahana Eden Population Outreach Models

    @copyright: 2015 (c) Sahana Software Foundation
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
"""

__all__ = ("OutreachAreaModel",
           "OutreachHouseholdModel",
           "OutreachReferralModel",
           "po_rheader",
           "po_organisation_onaccept",
           )

from ..s3 import *
from s3layouts import S3AddResourceLink

# =============================================================================
class OutreachAreaModel(S3Model):
    """ Model representing a mesh area for outreach work """

    names = ("po_area",
             "po_area_id",
             )

    def model(self):

        T = current.T
        db = current.db
        auth = current.auth

        define_table = self.define_table

        s3 = current.response.s3
        crud_strings = s3.crud_strings

        root_org = auth.root_org()
        ADMIN = current.session.s3.system_roles.ADMIN
        is_admin = auth.s3_has_role(ADMIN)

        # ---------------------------------------------------------------------
        # Area
        #
        tablename = "po_area"
        define_table(tablename,
                     self.super_link("doc_id", "doc_entity"),
                     Field("name",
                           requires = IS_NOT_EMPTY(),
                           ),
                     # @todo: link demographics?
                     self.gis_location_id(
                        widget = S3LocationSelector(points = False,
                                                    polygons = True,
                                                    feature_required = True,
                                                    ),
                     ),
                     # Only included to set realm entity:
                     self.org_organisation_id(default = root_org,
                                              readable = is_admin,
                                              writable = is_admin,
                                              ),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            label_create = T("Create Area"),
            title_display = T("Area Details"),
            title_list = T("Areas"),
            title_update = T("Edit Area"),
            label_list_button = T("List Areas"),
            label_delete_button = T("Delete Area"),
            msg_record_created = T("Area created"),
            msg_record_modified = T("Area updated"),
            msg_record_deleted = T("Area deleted"),
            msg_list_empty = T("No Areas currently registered"),
        )

        # Reusable field
        represent = S3Represent(lookup=tablename, show_link=True)
        area_id = S3ReusableField("area_id", "reference %s" % tablename,
                                  label = T("Area"),
                                  represent = represent,
                                  requires = IS_ONE_OF(db, "po_area.id",
                                                       represent,
                                                       ),
                                  sortby = "name",
                                  comment = S3AddResourceLink(f="area",
                                                              tooltip=T("Create a new area"),
                                                              ),
                                  )

        # Components
        self.add_components(tablename,
                            po_household = "area_id",
                            org_organisation = {"link": "po_organisation_area",
                                                "joinby": "area_id",
                                                "key": "organisation_id",
                                                "actuate": "hide",
                                                },
                            )

        levels = current.gis.get_relevant_hierarchy_levels()

        # Filters
        filter_widgets = [S3TextFilter(["name"]),
                          S3LocationFilter("location_id", levels = levels),
                          ]

        # @todo: reports

        # Table Configuration
        self.configure(tablename,
                       filter_widgets = filter_widgets,
                       summary = ({"common": True,
                                   "name": "add",
                                   "widgets": [{"method": "create"}],
                                   },
                                  {"name": "table",
                                   "label": "Table",
                                   "widgets": [{"method": "datatable"}]
                                   },
                                  {"name": "map",
                                   "label": "Map",
                                   "widgets": [{"method": "map",
                                                "ajax_init": True}],
                                   },
                                  ),
                       super_entity = "doc_entity",
                       )

        # ---------------------------------------------------------------------
        # Pass names back to global scope (s3.*)
        #
        return {"po_area_id": area_id,
                }

    # -------------------------------------------------------------------------
    @staticmethod
    def defaults():
        """ Safe defaults for names in case the module is disabled """

        dummy = S3ReusableField("dummy_id", "integer",
                                readable = False,
                                writable = False,
                                )

        return {"po_area_id": lambda **attr: dummy("area_id"),
                }

# =============================================================================
class OutreachHouseholdModel(S3Model):

    names = ("po_household",
             "po_household_id",
             "po_household_dwelling",
             "po_age_group",
             "po_household_member",
             "po_household_followup",
             "po_household_social",
             )

    def model(self):

        T = current.T
        db = current.db

        define_table = self.define_table
        super_link = self.super_link
        configure = self.configure

        s3 = current.response.s3
        crud_strings = s3.crud_strings

        # ---------------------------------------------------------------------
        # Household
        #
        tablename = "po_household"
        define_table(tablename,
                     super_link("doc_id", "doc_entity"),
                     super_link("pe_id", "pr_pentity"),
                     self.po_area_id(),
                     # @todo: inherit Lx from area and hide Lx (in area prep)
                     self.gis_location_id(label=T("Address")),
                     s3_date("date_visited",
                             default="now",
                             empty=False,
                             label=T("Date visited"),
                             ),
                     Field("followup", "boolean",
                           default = False,
                           label = T("Follow up"),
                           represent = s3_yes_no_represent,
                           ),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            label_create = T("Create Household"),
            title_display = T("Household Details"),
            title_list = T("Households"),
            title_update = T("Edit Household"),
            label_list_button = T("List Households"),
            label_delete_button = T("Delete Household"),
            msg_record_created = T("Household created"),
            msg_record_modified = T("Household updated"),
            msg_record_deleted = T("Household deleted"),
            msg_list_empty = T("No Households currently registered"),
        )

        # Reusable Field
        represent = po_HouseholdRepresent()
        household_id = S3ReusableField("household_id", "reference %s" % tablename,
                                       label = T("Household"),
                                       represent = represent,
                                       requires = IS_ONE_OF(db, "po_household.id",
                                                            represent,
                                                            ),
                                       sortby = "name",
                                       comment = S3AddResourceLink(f="household",
                                                                   tooltip=T("Create a new household"),
                                                                   ),
                                       )

        # Filter Widgets
        filter_widgets = [S3TextFilter(("household_member.person_id$first_name",
                                        "household_member.person_id$middle_name",
                                        "household_member.person_id$last_name",
                                        "location_id$addr_street",
                                        ),
                                        label = T("Search"),
                                        comment = T("Search by Address or Name of Household Member"),
                                       ),
                          S3OptionsFilter("area_id",
                                          #hidden = True,
                                          ),
                          S3DateFilter("date_visited",
                                       label = T("Date visited"),
                                       hidden = True,
                                       ),
                          S3OptionsFilter("followup",
                                          cols = 2,
                                          hidden = True,
                                          ),
                          S3DateFilter("household_followup.followup_date",
                                       label = T("Follow-up Date"),
                                       hidden = True,
                                       ),
                          S3OptionsFilter("organisation_household.organisation_id",
                                          hidden = True,
                                          ),
                          ]

        # List fields
        list_fields = ("area_id",
                       "location_id",
                       "date_visited",
                       "followup",
                       "household_followup.followup_date",
                       "organisation_household.organisation_id",
                       "comments",
                       )

        # Reports
        report_axes = ["area_id",
                       "followup",
                       "organisation_household.organisation_id",
                       "household_followup.evaluation",
                       ]
        reports = ((T("Number of Households Visited"), "count(id)"),
                   )

        configure(tablename,
                  create_next = self.household_create_next,
                  filter_widgets = filter_widgets,
                  list_fields = list_fields,
                  onaccept = self.household_onaccept,
                  report_options = {"rows": report_axes,
                                    "cols": report_axes,
                                    "fact": reports,
                                    "defaults": {
                                            "rows": "area_id",
                                            "cols": "followup",
                                            "fact": "count(id)",
                                        }
                                    },
                  super_entity = ("doc_entity", "pr_pentity"),
                  )

        # Components
        self.add_components(tablename,
                            pr_person = {"link": "po_household_member",
                                         "joinby": "household_id",
                                         "key": "person_id",
                                         "actuate": "replace",
                                         },
                            po_household_dwelling = {"joinby": "household_id",
                                                     "multiple": False,
                                                     },
                            po_household_social = {"joinby": "household_id",
                                                   "multiple": False,
                                                   },
                            po_household_followup = {"joinby": "household_id",
                                                     "multiple": False,
                                                     },
                            po_organisation_household = "household_id",
                            )

        # ---------------------------------------------------------------------
        # Household Members
        #
        tablename = "po_household_member"
        define_table(tablename,
                     household_id(),
                     self.pr_person_id(),
                     s3_comments(),
                     *s3_meta_fields())

        # ---------------------------------------------------------------------
        # Household Member Age Groups (under 18,18-30,30-55,56-75,75+)
        #
        age_groups = ("<18", "18-30", "30-55", "56-75", "75+")
        tablename = "po_age_group"
        define_table(tablename,
                     self.pr_person_id(),
                     Field("age_group",
                           label = T("Age Group"),
                           requires = IS_EMPTY_OR(IS_IN_SET(age_groups)),
                           ),
                     *s3_meta_fields())

        # ---------------------------------------------------------------------
        # Dwelling
        #
        dwelling_type = {"U": T("Unit"),
                         "H": T("House"),
                         "A": T("Apartment"),
                         "S": T("Supervised House"),
                         "O": T("Other"),
                         }
        type_of_use = {"S": T("Owner-occupied"),
                       "R": T("Renting"),
                       "B": T("Boarding"),
                       "O": T("Other"),
                       }
        repair_status = {"W": T("waiting"),
                         "R": T("rebuild"),
                         "C": T("completed"),
                         "N": T("not required"),
                         "O": T("other"),
                         }

        tablename = "po_household_dwelling"
        define_table(tablename,
                     household_id(),
                     Field("dwelling_type",
                           label = T("Type of Dwelling"),
                           represent = S3Represent(options=dwelling_type),
                           requires = IS_EMPTY_OR(IS_IN_SET(dwelling_type)),
                           ),
                     Field("type_of_use",
                           label = T("Type of Use"),
                           represent = S3Represent(options=type_of_use),
                           requires = IS_EMPTY_OR(IS_IN_SET(type_of_use)),
                           ),
                     Field("repair_status",
                           label = T("Stage of Repair"),
                           represent = S3Represent(options=repair_status),
                           requires = IS_EMPTY_OR(IS_IN_SET(repair_status)),
                           ),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            title_update = T("Edit Dwelling Data"),
        )

        # ---------------------------------------------------------------------
        # Social Information
        #
        languages = dict(IS_ISO639_2_LANGUAGE_CODE.language_codes())

        tablename = "po_household_social"
        define_table(tablename,
                     household_id(),
                     Field("language",
                           label = T("Main Language"),
                           represent = S3Represent(options=languages),
                           requires = IS_ISO639_2_LANGUAGE_CODE(select=None,
                                                                sort=True,
                                                                ),
                           ),
                     Field("community", "text",
                           label = T("Community Connections"),
                           ),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            title_update = T("Edit Social Information"),
        )

        # ---------------------------------------------------------------------
        # Follow-up Details
        #
        evaluation = {"B": T("better"),
                      "S": T("same"),
                      "W": T("worse"),
                      }

        twoweeks = s3.local_date + datetime.timedelta(days=14)

        tablename = "po_household_followup"
        define_table(tablename,
                     household_id(),
                     Field("followup_required",
                           label = T("Follow-up required"),
                           ),
                     s3_date("followup_date",
                             label = T("Date for Follow-up"),
                             default = twoweeks,
                             past = 0,
                             ),
                     Field("followup", "text",
                           label = T("Follow-up made"),
                           ),
                     Field("evaluation",
                           label = T("Evaluation"),
                           represent = S3Represent(options=evaluation),
                           requires = IS_EMPTY_OR(IS_IN_SET(evaluation)),
                           ),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            title_update = T("Edit Follow-up Details"),
        )

        configure(tablename,
                  deletable = False,
                  )

        # ---------------------------------------------------------------------
        # Pass names back to global scope (s3.*)
        #
        return {"po_household_id": household_id,
                }

    # -------------------------------------------------------------------------
    @staticmethod
    def defaults():
        """ Safe defaults for names in case the module is disabled """

        dummy = S3ReusableField("dummy_id", "integer",
                                readable = False,
                                writable = False)

        return {"po_household_id": lambda **attr: dummy("household_id"),
                }

    # -------------------------------------------------------------------------
    @staticmethod
    def household_create_next(r):
        """ Determine where to go next after creating a new household """

        post_vars = r.post_vars
        next_vars = S3Method._remove_filters(r.get_vars)
        next_vars.pop("w", None)

        follow_up = "followup" in post_vars and post_vars["followup"]

        if r.function == "area":
            if follow_up:
                return URL(f="household",
                           args=["[id]", "contact"],
                           vars=next_vars,
                           )
            else:
                return r.url(method="",
                             component="household",
                             vars=next_vars,
                             )
        else:
            if follow_up:
                return r.url(target="[id]",
                             component="contact",
                             method="",
                             vars=next_vars,
                             )
            else:
                return r.url(method="summary",
                             id="",
                             vars=next_vars,
                             )

    # -------------------------------------------------------------------------
    @staticmethod
    def household_onaccept(form):
        """ Onaccept-routine for households """

        formvars = form.vars
        try:
            record_id = formvars.id
        except AttributeError:
            return

        # Auto-create a followup component if household.followup is True
        s3db = current.s3db
        htable = s3db.po_household
        ftable = s3db.po_household_followup

        left = ftable.on((ftable.household_id == htable.id) & \
                         (ftable.deleted != True))
        row = current.db(htable.id == record_id).select(htable.id,
                                                        htable.followup,
                                                        ftable.id,
                                                        left=left,
                                                        limitby=(0, 1)).first()
        if row and row[htable.followup] and not row[ftable.id]:
            ftable.insert(household_id=row[htable.id])

# =============================================================================
class OutreachReferralModel(S3Model):
    """ Model to track referrals of households to organisations """

    names = ("po_referral_organisation",
             "po_organisation_area",
             "po_organisation_household",
             )

    def model(self):

        T = current.T
        db = current.db

        define_table = self.define_table
        configure = self.configure

        s3 = current.response.s3
        crud_strings = s3.crud_strings

        organisation_id = self.org_organisation_id

        # Organisation Represent should link to po/organisation
        org_link = URL(c="po", f="organisation", args="[id]")
        org_represent = self.org_OrganisationRepresent(show_link=True,
                                                       linkto=org_link,
                                                       )
        # Organisation AddResourceLink should go to po/organisation
        ADD_ORGANISATION = T("Create Agency")
        tooltip = T("If you don't see the Agency in the list, you can add a new one by clicking link 'Create Agency'.")
        org_comment = S3AddResourceLink(c="po", f="organisation",
                                        label=ADD_ORGANISATION,
                                        title=ADD_ORGANISATION,
                                        tooltip=tooltip,
                                        )

        # ---------------------------------------------------------------------
        # Referral Agency (context link table), currently not visible
        #
        tablename = "po_referral_organisation"
        define_table(tablename,
                     organisation_id(represent=org_represent,
                                     comment=org_comment,
                                     ),
                     #s3_comments(),
                     *s3_meta_fields())

        # ---------------------------------------------------------------------
        # Areas Served by a Referral Agency
        #
        tablename = "po_organisation_area"
        define_table(tablename,
                     # @todo: AddResourceLink should go to po/organisation
                     organisation_id(label=T("Agency"),
                                     represent=org_represent,
                                     comment=org_comment,
                                     ),
                     self.po_area_id(),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            label_create = T("Add Agency"),
            title_update = T("Edit Referral Agency"),
            label_list_button = T("List Agencies"),
            label_delete_button = T("Remove Agency"),
        )

        # ---------------------------------------------------------------------
        # Referral Household=>Agency
        #
        tablename = "po_organisation_household"
        define_table(tablename,
                     # @todo: AddResourceLink should go to po/organisation
                     organisation_id(label=T("Referral Agency"),
                                     represent=org_represent,
                                     comment=org_comment,
                                     ),
                     self.po_household_id(),
                     s3_date(default="now",
                             label=T("Date Referral Made"),
                             ),
                     s3_comments(),
                     *s3_meta_fields())

        # CRUD Strings
        crud_strings[tablename] = Storage(
            label_create = T("Add Referral"),
            title_update = T("Edit Referral Details"),
            label_delete_button = T("Delete Referral"),
        )

        # Table Configuration
        configure(tablename,
                  orderby = "%s.date desc" % tablename,
                  list_fields = ("date",
                                 "organisation_id",
                                 "household_id",
                                 "comments",
                                 ),
                  )

# =============================================================================
class po_HouseholdRepresent(S3Represent):

    def __init__(self, show_link=True):
        """
            Constructor

            @param show_link: whether to add a URL to representations
        """

        super(po_HouseholdRepresent, self).__init__(
                                        lookup = "po_household",
                                        show_link = show_link)

        self.location_represent = \
                current.s3db.gis_LocationRepresent(address_only=True,
                                                   show_link=False,
                                                   )

    # -------------------------------------------------------------------------
    def lookup_rows(self, key, values, fields=[]):
        """
            Custom rows lookup

            @param key: the key Field
            @param values: the values
            @param fields: unused (retained for API compatibility)
        """

        s3db = current.s3db
        table = self.table

        count = len(values)
        if count == 1:
            query = (key == values[0])
        else:
            query = key.belongs(values)
        rows = current.db(query).select(table.id,
                                        table.location_id,
                                        limitby = (0, count),
                                        )
        self.queries += 1

        # Bulk-represent locations
        location_id = str(table.location_id)
        location_ids = [row[location_id] for row in rows]
        if location_ids:
            self.location_represent.bulk(location_ids, show_link=False)

        return rows

    # -------------------------------------------------------------------------
    def represent_row(self, row):
        """
            Represent a row

            @param row: the Row
        """

        # Represent household as its address
        return self.location_represent(row.location_id)

# =============================================================================
def po_rheader(r, tabs=[]):

    if r.representation != "html":
        # RHeaders only used in interactive views
        return None

    tablename = r.tablename
    record = r.record

    rheader = None
    rheader_fields = []

    if record:
        T = current.T

        if tablename == "po_area":

            # @todo: hide "Referral Agencies" per deployment setting
            if not tabs:
                tabs = [(T("Basic Details"), ""),
                        (T("Households"), "household"),
                        (T("Referral Agencies"), "organisation"),
                        (T("Documents"), "document"),
                        ]

            rheader_fields = [["name"],
                              ]

        elif tablename == "po_household":

            if not tabs:
                tabs = [(T("Basic Details"), "")]
                if record.followup:
                    tabs.extend([(T("Contact Information"), "contact"),
                                 (T("Social Information"), "household_social"),
                                 (T("Members"), "person"),
                                 (T("Dwelling"), "household_dwelling"),
                                 (T("Follow-up Details"), "household_followup"),
                                 (T("Referrals"), "organisation_household"),
                                 ])

            rheader_fields = [["area_id"],
                              ["location_id"],
                              ]

        elif tablename == "org_organisation":

            # @todo: hide "Areas Served" per deployment setting
            if not tabs:
                tabs = [(T("Basic Details"), ""),
                        (T("Areas Served"), "area"),
                        (T("Referrals"), "organisation_household"),
                        ]

            rheader_fields = [["name"],
                              ]

        rheader = S3ResourceHeader(rheader_fields, tabs)(r)

    return rheader

# =============================================================================
def po_organisation_onaccept(form):
    """
        Create a po_referral_organisation record onaccept of
        an org_organisation to link it to this module.

        @param form: the form
    """

    formvars = form.vars
    try:
        organisation_id = formvars.id
    except AttributeError:
        return

    rtable = current.s3db.po_referral_organisation
    query = (rtable.organisation_id == organisation_id) & \
            (rtable.deleted != True)
    row = current.db(query).select(rtable.id, limitby=(0, 1)).first()
    if not row:
        rtable.insert(organisation_id=organisation_id)

# END =========================================================================
