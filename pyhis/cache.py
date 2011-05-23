"""
    PyHIS.cache
    ~~~~~~~~~~

    SQLAlchemy-based caching support
"""
#----------------------------------------------------------------------------
# cache should work like this:
#  1. check in-memory cache, if found return the obj
#  2. check database, if found return the obj and update in-memory cache
#  3. make new network request, update database with results
#----------------------------------------------------------------------------
import logging
import platform
import warnings

import pandas
from sqlalchemy import create_engine, func, Table
from sqlalchemy.schema import ForeignKey, Index, UniqueConstraint
from sqlalchemy import (Column, Boolean, Integer, Text, String, Float,
                        DateTime, Enum)
from sqlalchemy.ext.declarative import (declarative_base, declared_attr,
                                        synonym_for)
from sqlalchemy.orm import relationship, backref, sessionmaker, synonym
from sqlalchemy.orm.exc import NoResultFound

import pyhis
from pyhis import waterml

CACHE_DATABASE_FILE = "/tmp/pyhis_cache.db"
ECHO_SQLALCHEMY = False

#XXX: this should be programmatically generated in some clever way
#     (e.g. based on some config)
USE_CACHE = True
USE_SPATIAL = False

# configure logging
LOG_FORMAT = '%(message)s'
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
log = logging.getLogger(__name__)


try:
    from geoalchemy import (GeometryColumn, GeometryDDL, Point,
                            WKTSpatialElement)
    from pysqlite2 import dbapi2 as sqlite
except ImportError:
    from sqlite3 import dbapi2 as sqlite
    USE_SPATIAL = False

Session = sessionmaker(autocommit=False, autoflush=False)
Base = declarative_base()


def init_cache(cache_database_file=CACHE_DATABASE_FILE):
    global engine
    global db_session
    global _cache

    cache_database_uri = 'sqlite:///' + cache_database_file

    # to use geoalchemy with spatialite, the libspatialite library has to
    # be loaded as an extension
    engine = create_engine(cache_database_uri, convert_unicode=True,
                           module=sqlite, echo=ECHO_SQLALCHEMY)

    if USE_SPATIAL:
        if "ARCH" in platform.uname()[2]:
            LIBSPATIALITE_LOCATION="select load_extension('/usr/lib/libspatialite.so.1')"
        else:
            LIBSPATIALITE_LOCATION="select load_extension('/usr/lib/libspatialite.so.2')"

        if 'sqlite' in cache_database_uri:
            connection = engine.raw_connection().connection
            connection.enable_load_extension(True)
            engine.execute(LIBSPATIALITE_LOCATION)

    # bind new engine to the db_session and Base metadata objects
    db_session = Session(bind=engine)
    Base.metadata.bind = engine

    # in-memory cache dict that will keep us from creating multiple cache
    # objects that represent the same DB objects
    _cache = {
        'source': {},       # key: url
        'site': {},         # key: (source_url, network, site_code)
        'timeseries': {},   # key: (source_url, network, site_code, variable_code)
        'variable': {},     # key: (vocabulary, variable_code)
        'units': {},        # key: (name, code)
        }

    log.info('cache initialized with database: %s' % cache_database_uri)

    # there is probably a cleaner way to do this, but the idea is to
    # try to run create_all_tables; this won't work if the DB objects
    # haven't been initialized (i.e. on import), but if init_cache is
    # called post-import (meaning the database file is changed), then
    # the tables need to be created
    try:
        create_all_tables()
    except NameError:
        pass

init_cache()


#----------------------------------------------------------------------------
# cache SQLAlchemy models
#----------------------------------------------------------------------------


def create_cache_obj(db_model, cache_key, lookup_key_func, db_lookup_func):
    """
    Create a cache object that searches the in-memory cache dict for
    the key returned by calling lookup_key_func. If no object is found
    matching this key, then call db_lookup_func to search the
    database. If no result is found in the database, then create a new
    database object, save it to the database and return the new
    object.
    """

    class CacheObj(object):
        def __new__(cls, *args, **kwargs):
            #: add a check for an auto_commit kwarg that determines
            #: whether or not to automatically commit an obj to the db
            #: cache (if it doesn't already exist in db)
            auto_commit = kwargs.pop('auto_commit', True)
            skip_db_lookup = kwargs.pop('skip_db_lookup', False)

            lookup_key = lookup_key_func(*args, **kwargs)
            if lookup_key in _cache[cache_key]:
                return _cache[cache_key][lookup_key]

            if skip_db_lookup:
                db_instance = db_model(*args, **kwargs)
                if auto_commit:
                    db_session.add(db_instance)
                    db_session.commit()
            else:
                try:
                    db_instance = db_lookup_func(*args, **kwargs)
                except NoResultFound, SkipDBLookup:
                    db_instance = db_model(*args, **kwargs)
                    if auto_commit:
                        db_session.add(db_instance)
                        db_session.commit()

            _cache[cache_key][lookup_key] = db_instance
            return db_instance

    return CacheObj


class DBCacheDatesMixin(object):
    """
    Mixin class for keeping track of cache times
    """
    last_refreshed = Column(DateTime, default=func.now(),
                            onupdate=func.now())


class DBSource(Base):
    __tablename__ = 'source'

    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True)
    sites = relationship('DBSite', backref='source')
    last_get_sites = Column(DateTime)

    def __init__(self, source=None, url=None):
        if not source is None:
            self._from_pyhis(source)
        else:
            self.url = url

    def _from_pyhis(self, pyhis_source):
        self.url = pyhis_source.url

    def to_pyhis(self):
        return pyhis.Source(wsdl_url=self.url)


def _source_lookup_key_func(source=None, url=None):
    if not source is None:
        return source.url
    if url:
        return url


def _source_db_lookup_func(source=None, url=None):
    if not source is None:
        url = source.url

    return db_session.query(DBSource).filter_by(url=url).one()

CacheSource = create_cache_obj(DBSource, 'source', _source_lookup_key_func,
                               _source_db_lookup_func)


class DBSiteMixin(object):
    """
    Using inheritance with the SQLAlchemy declarative pattern is done
    via Mixin Classes. This class provides a Mixin Class for the Site
    DB model that can be used for both spatial and non-spatial
    database site models.
    """
    __tablename__ = 'site'

    id = Column(Integer, primary_key=True)
    site_id = Column(String)
    name = Column(String)
    code = Column(String)
    network = Column(String)

    @declared_attr
    def source_id(cls):
        return Column(Integer, ForeignKey('source.id'), nullable=False)

    @declared_attr
    def timeseries_list(cls):
        return relationship('DBTimeSeries', backref='site', lazy='subquery')

    @declared_attr
    def __table_args__(cls):
        return (
            UniqueConstraint('network', 'code'),
#            Index('idx_%s_network_code' % cls.__tablename__,
#                  'network', 'code'),
            {}
            )


    # populated by backref:
    #   source = DBSource

    def _from_pyhis(self, pyhis_site):
        self.site_id = pyhis_site.id
        self.name = pyhis_site.name
        self.code = pyhis_site.code
        self.network = pyhis_site.network
        self.latitude = pyhis_site.latitude
        self.longitude = pyhis_site.longitude
        self.source = CacheSource(pyhis_site.source)

    def to_pyhis(self, source=None):
        # because every site needs a reference to a source, a source
        # object *should* be passed to this method to avoid an extra
        # object being created. Some proxy voodoo could that could go
        # on here to prevent this. For now, assume that if we didn't
        # get a source, then just make a new one.
        if source is None:
            source = self.source.to_pyhis()

        return pyhis.Site(
            code=self.code,
            name=self.name,
            id=self.site_id,
            network=self.network,
            latitude=self.latitude,
            longitude=self.longitude,
            source=source)


if USE_SPATIAL:
    class DBSite(Base, DBSiteMixin, DBCacheDatesMixin):
        geom = GeometryColumn(Point(2))

        def __init__(self, site=None, site_id=None, name=None, code=None,
                     network=None, source=None, latitude=None, longitude=None):
            if site:
                self._from_pyhis(site)
            else:
                self.site_id = site_id
                self.name = name
                self.code = code
                self.network = network
                self.source = source
                self.latitude = latitude
                self.longitude = longitude

        @property
        def latitude(self):
            x, y = self.geom.coords(db_session)
            return y

        @latitude.setter
        def latitude(self, latitude):
            wkt_point = "POINT(%f %f)" % (self.longitude, latitude)
            self.geom = WKTSpatialElement(wkt_point)

        @property
        def longitude(self):
            x, y = self.geom.coords(db_session)
            return x

        @longitude.setter
        def longitude(self, longitude):
            wkt_point = "POINT(%f %f)" % (longitude, self.latitude)
            self.geom = WKTSpatialElement(wkt_point)

    GeometryDDL(DBSite.__table__)

else:
    class DBSite(Base, DBSiteMixin, DBCacheDatesMixin):

        latitude = Column(Float)
        longitude = Column(Float)

        def __init__(self, site=None, site_id=None, name=None, code=None,
                     network=None, source=None, latitude=None, longitude=None):
            if site:
                self._from_pyhis(site)
            else:
                self.site_id = site_id
                self.name = name
                self.code = code
                self.network = network
                self.source = source
                self.latitude = latitude
                self.longitude = longitude


def _site_lookup_key_func(site=None, network=None, code=None):
    if site:
        return (site.network, site.code)
    if network and code:
        return (network, code)


def _site_db_lookup_func(site=None, network=None, code=None):
    if site:
        network = site.network
        code = site.code

    return db_session.query(DBSite).filter_by(network=network,
                                              code=code).one()

CacheSite = create_cache_obj(DBSite, 'site', _site_lookup_key_func,
                             _site_db_lookup_func)


class DBTimeSeries(Base, DBCacheDatesMixin):
    __tablename__ = 'timeseries'

    id = Column(Integer, primary_key=True)
    begin_datetime = Column(DateTime)
    end_datetime = Column(DateTime)
    method = Column(String)
    quality_control_level = Column(String)
    site_id = Column(Integer, ForeignKey('site.id'), nullable=False)
    variable_id = Column(Integer, ForeignKey('variable.id'), nullable=False)
    value_count = Column(Integer)

    variable = relationship('DBVariable', lazy='subquery')
    values = relationship('DBValue', order_by="DBValue.timestamp",
                          cascade='save-update, merge, delete, delete-orphan',
                          lazy='subquery')

    # populated by backref:
    #   site = DBSite

    def __init__(self, timeseries=None, begin_datetime=None,
                 end_datetime=None, method=None, quality_control_level=None,
                 site=None, variable=None, value_count=None):
        if timeseries:
            self._from_pyhis(timeseries)
        else:
            self.begin_datetime = begin_datetime
            self.end_datetime = end_datetime
            self.method = method
            self.quality_control_level = quality_control_level
            self.site = site
            self.variable = variable
            self.value_count = value_count

    def _from_pyhis(self, pyhis_timeseries):
        self.method = pyhis_timeseries.method
        self.begin_datetime = pyhis_timeseries.begin_datetime
        self.end_datetime = pyhis_timeseries.end_datetime
        self.quality_control_level = pyhis_timeseries.quality_control_level
        self.variable = CacheVariable(pyhis_timeseries.variable)
        self.site = CacheSite(pyhis_timeseries.site, auto_commit=False)
        self.value_count = pyhis_timeseries.value_count

    def to_pyhis(self, site=None, variable=None):
        # as with DBSite.to_pyhis()...
        # because every timeseries needs a reference to a site and a
        # variable, these objects *should* be passed to this method to
        # avoid extra objects being created. Some proxy voodoo could
        # that could go on here to prevent this. For now, assume that
        # if we didn't get a reference to an existing object, then
        # just make a new one.
        if site is None:
            site = self.site.to_pyhis()

        if variable is None:
            variable = self.variable.to_pyhis()

        return pyhis.TimeSeries(
            variable=variable,
            method=self.method,
            quality_control_level=self.quality_control_level,
            begin_datetime=self.begin_datetime,
            end_datetime=self.end_datetime,
            site=site,
            value_count=self.value_count)


def _timeseries_lookup_key_func(timeseries=None, url=None, network=None,
                                site_code=None, variable=None):
    if timeseries:
        return (timeseries.site.source.url, timeseries.site.network,
                timeseries.site.code, timeseries.variable.code)
    if url and network and site_code and variable:
        return (url, network, site_code, variable)


def _timeseries_db_lookup_func(timeseries=None, network=None, site_code=None,
                                variable=None):
    if timeseries:
        network = timeseries.site.network
        site_code = timeseries.site.code
        variable = db_session.query(DBVariable).filter_by(
            vocabulary=timeseries.variable.vocabulary,
            code=timeseries.variable.code).one()

    if network and site_code and variable:
        variable_code = variable.code

    site = db_session.query(DBSite).filter_by(network=network,
                                              code=site_code).one()
    return db_session.query(DBTimeSeries).filter_by(site=site,
                                                    variable=variable).one()

CacheTimeSeries = create_cache_obj(DBTimeSeries, 'timeseries',
                                   _timeseries_lookup_key_func,
                                   _timeseries_db_lookup_func)


class DBUnits(Base, DBCacheDatesMixin):
    __tablename__ = 'units'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    abbreviation = Column(String)
    code = Column(String)

    def __init__(self, units=None, name=None, abbreviation=None, code=None):
        if units:
            self._from_pyhis(units)
        else:
            self.name = name
            self.abbreviation = abbreviation
            self.code = code

    def _from_pyhis(self, pyhis_units):
        self.name = pyhis_units.name
        self.abbreviation = pyhis_units.abbreviation
        self.code = pyhis_units.code

    def to_pyhis(self):
        return pyhis.Units(
            name=self.name,
            abbreviation=self.abbreviation,
            code=self.code)


def _units_lookup_key_func(units=None, name=None, code=None):
    if units:
        return (units.name, units.code)
    if name and code:
        return (name, code)


def _units_db_lookup_func(units=None, name=None, code=None):
    if units:
        name = units.name
        code = units.code

    return db_session.query(DBUnits).filter_by(name=name,
                                               code=code).one()

CacheUnits = create_cache_obj(DBUnits, 'units', _units_lookup_key_func,
                              _units_db_lookup_func)


class DBValue(Base, DBCacheDatesMixin):
    __tablename__ = 'value'

    id = Column(Integer, primary_key=True)
    value = Column(Float)
    timestamp = Column(DateTime)
    timeseries_id = Column(Integer, ForeignKey('timeseries.id'),
                           nullable=False)
    timeseries = relationship('DBTimeSeries', lazy='subquery')

    def __init__(self, value=None, timestamp=None, timeseries=None):
        self.value = value
        self.timestamp = timestamp
        self.timeseries = timeseries


class DBVariable(Base, DBCacheDatesMixin):
    __tablename__ = 'variable'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    code = Column(String)
    variable_id = Column(String)
    vocabulary = Column(String)
    no_data_value = Column(String)
    units_id = Column(Integer, ForeignKey('units.id'))

    units = relationship('DBUnits', lazy='subquery')

    def __init__(self, variable=None, name=None, code=None, variable_id=None,
                 vocabulary=None, no_data_value=None, site_id=None,
                 units=None):
        if not variable is None:
            self._from_pyhis(variable)
        else:
            self.name = name
            self.code = code
            self.variable_id = variable_id
            self.vocabulary = vocabulary
            self.no_data_value = no_data_value
            self.units = units

    def _from_pyhis(self, pyhis_variable):
        self.name = pyhis_variable.name
        self.code = pyhis_variable.code
        self.variable_id = pyhis_variable.id
        self.vocabulary = pyhis_variable.vocabulary
        self.no_data_value = pyhis_variable.no_data_value
        self.units = CacheUnits(pyhis_variable.units)

    def to_pyhis(self):
        return pyhis.Variable(
            name=self.name,
            code=self.code,
            id=self.variable_id,
            vocabulary=self.vocabulary,
            units=self.units.to_pyhis())


def _variable_lookup_key_func(variable=None, vocabulary=None, code=None):
    if variable:
        return (variable.vocabulary, variable.code)
    if vocabulary and code:
        return (vocabulary, code)


def _variable_db_lookup_func(variable=None, vocabulary=None, code=None):
    if variable:
        vocabulary = variable.vocabulary
        code = variable.code

    return db_session.query(DBVariable).filter_by(vocabulary=vocabulary,
                                                  code=code).one()

CacheVariable = create_cache_obj(DBVariable, 'variable',
                                 _variable_lookup_key_func,
                                 _variable_db_lookup_func)


# run create_all to make sure the database tables are all there
def create_all_tables():
    Base.metadata.create_all()

create_all_tables()



#----------------------------------------------------------------------------
# cache functions
#----------------------------------------------------------------------------


def cache_all(source_url):
    """
    Cache all available data for a source
    """
    source = pyhis.Source(source_url)
    total_sites = len(source.sites.values())

    for i, site in enumerate(source.sites.values(), 1):
        # this could be improved by not having to create the
        # dataframe object
        try:
            log.info('caching values for site %s/%s: %s' %
                     i, total_sites, site.name)
            df = site.dataframe

            # delete dataframe object and site timeseries dict
            # (TimeSeries objects each have a series object) to free
            # up some memory
            del df
            del site._dataframe
            del site._timeseries_dict
        except Exception as e:
            warnings.warn("There was a problem getting values for "
                          "%s, skipping..." % (site))


def get_sites_for_source(source):
    """
    return a dict of pyhis.Site objects for a given source. The source
    can be either a string representing the url or a pyhis.Source
    object
    """
    cached_source = CacheSource(source)

    if not cached_source.last_get_sites:
        site_list = waterml.get_sites_for_source(cached_source.to_pyhis())

        # since the sites don't exist in the db yet, just
        # instantiating them via the CacheSite constructor will
        # queue them to be saved to the db and (update them in the
        # in-memory cache)

        skip_db_lookup = bool(len(cached_source.sites) == 0)
        cache_sites = [CacheSite(site, auto_commit=False,
                                 skip_db_lookup=skip_db_lookup)
                       for site in site_list.values()]

        # add queued sites to cache
        db_session.add_all(cache_sites)

        # update cached_source.last_get_sites
        cached_source.last_get_sites = func.now()

        # commit
        db_session.commit()

    return dict([(cached_site.code,
                  cached_site.to_pyhis(source))
                 for cached_site in cached_source.sites])


def get_site(source, network, code):
    """
    return a pyhis.Site for a given source, network and site_code. The
    source can be either a string representing the url or a
    pyhis.Source object
    """
    cached_source = CacheSource(source)

    try:
        cached_site = db_session.query(DBSite).filter_by(
            source=cached_source, network=network, code=code).one()
    except NoResultFound:
        site = pyhis.core.Site(network=network, code=code, source=source)

        # create a CacheSite so the site gets cached
        cached_site = CacheSite(site)
    return cached_site.to_pyhis()


def get_timeseries_dict_for_site(site):
    """
    returns a dict of pyhis.TimeSeries objects with the variable code
    as keys for a given site and variable_code
    """
    cached_site = CacheSite(site)

    if len(cached_site.timeseries_list) == 0:
        timeseries_dict = waterml.\
                          get_timeseries_dict_for_site(cached_site.to_pyhis())
        # Since the timeseries don't exist in the db yet, just
        # instantiating them via the CacheSite constructor will save
        # them to the db and (update them in the in-memory
        # cache). This can be expensive if there are a lot of
        # timeseries objects, so we defer commits until they have all
        # been instantiated and can be commited at once.
        timeseries_list = [CacheTimeSeries(timeseries, auto_commit=False,
                                           skip_db_lookup=True)
                           for timeseries in timeseries_dict.values()]
        db_session.add_all(timeseries_list)
        db_session.commit()

        return timeseries_dict

    # else:
    timeseries_list = [cached_timeseries.to_pyhis(site=site)
                       for cached_timeseries in cached_site.timeseries_list]
    return dict([(timeseries.variable.code, timeseries)
                 for timeseries in timeseries_list])


def get_series_and_quantity_for_timeseries(
    timeseries, check_for_updates=False, defer_commit=False):
    """
    returns a tuple where the first element is a pandas.Series
    containing the timeseries data for the timeseries and the second
    element is the python quantity that corresponds the unit for the
    variable. Takes a suds WaterML TimeSeriesResponseType object.
    """
    cached_timeseries = CacheTimeSeries(timeseries)

    if check_for_updates or \
           _need_to_update_timeseries(cached_timeseries, timeseries):
        series, quantity = \
                waterml.get_series_and_quantity_for_timeseries(timeseries)

        # cache all the values; we do this "manually" because
        # there isn't much point in caching values; it's just
        # wasteful memory-wise
        db_values = [DBValue(value=value, timestamp=timestamp,
                             timeseries=cached_timeseries)
                     for timestamp, value in series.iteritems()]

        if len(db_values) != cached_timeseries.value_count:
            warnings.warn("value_count (%s) doesn't match number of values (%s) for %s:%s" %
                          (len(db_values), cached_timeseries.value_count,
                           cached_timeseries.site.name,
                           cached_timeseries.variable.code))

        cached_timeseries.values = db_values

        if not defer_commit:
            db_session.commit()

        return series, quantity


    series_dict = dict([(cached_value.timestamp, cached_value.value)
                        for cached_value in cached_timeseries.values])
    series = pandas.Series(series_dict)

    # look for the unit code in the unit_quantities dict, if it's not
    # there just use the unit name
    unit_code = cached_timeseries.variable.units.code
    try:
        quantity = pyhis.unit_quantities[unit_code]
    except KeyError:
        quantity = cached_timeseries.variable.units.name
        variable_code = cached_timeseries.variable.code
        warnings.warn("Unit conversion not available for %s: %s [%s]" %
                      (variable_code, quantity, unit_code))

    return series, quantity


def _need_to_update_timeseries(cached_timeseries, pyhis_timeseries):
    number_of_cached_values = len(cached_timeseries.values)
    if number_of_cached_values != pyhis_timeseries.value_count:
        return True

    try:
        if cached_timeseries.values[0].timestamp != pyhis_timeseries.begin_datetime \
           or cached_timeseries.values[-1].timestamp != pyhis_timeseries.end_datetime:
            return True
    except KeyError:
        return True

    return False
