import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import time
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiLineString, Polygon, MultiPolygon
from shapely import wkt
from sqlalchemy import create_engine, text
from geoalchemy2 import Geometry, WKTElement
import configparser
import os

# Read configuration from config.ini
config = configparser.ConfigParser()
config.read('config.ini')

username = config['database']['username']
password = config['database']['password']
host = config['database']['host']
port = config['database']['port']
database_name = config['database']['database_name']
schema = config['database']['schema']
layer_point = config['database']['layer_point']
layer_line = config['database']['layer_line']
layer_polygon = config['database']['layer_polygon']
db_url = f'postgresql://{username}:{password}@{host}:{port}/{database_name}'

geometry_bounds_wkt = config['api']['geometry_bounds']
geometry_bounds = wkt.loads(geometry_bounds_wkt)
start_datum_str = config['api']['start_datum']

# Convert start_datum to datetime
start_datum = datetime.strptime(start_datum_str, '%Y-%m-%d')

# Constants
API_ENDPOINT = "https://repository.overheid.nl/sru"
NAMESPACES = {
    'sru': "http://docs.oasis-open.org/ns/search-ws/sruResponse",
    'gzd': "http://standaarden.overheid.nl/sru",
    'dcterms': "http://purl.org/dc/terms/",
    'c': "http://standaarden.overheid.nl/collectie/",
    'overheidwetgeving': "http://standaarden.overheid.nl/wetgeving/",
    'overheid': "http://standaarden.overheid.nl/owms/terms/"
}
FIELDS_TO_EXTRACT = [
    'dcterms:identifier', 'dcterms:title', 'dcterms:type', 'dcterms:creator',
    'dcterms:modified', 'overheid:authority', 'dcterms:available',
    'dcterms:hasVersion', 'dcterms:subject', 'dcterms:abstract',
    'dcterms:publisher', 'c:product-area', 'c:content-area',
    'overheidwetgeving:activiteit', 'overheidwetgeving:jaargang',
    'overheidwetgeving:organisatietype', 'overheidwetgeving:publicatienummer',
    'overheidwetgeving:publicatienaam'
]


def timer_decorator(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        elapsed_time = time.time() - start_time
        print(f"Elapsed time for {func.__name__}: {elapsed_time:.2f} seconds")
        return result

    return wrapper


def validate_geometries(gdf):
    """Validate geometries in a GeoDataFrame and remove invalid ones."""
    valid_gdf = gdf[gdf.is_valid]
    invalid_gdf = gdf[~gdf.is_valid]
    print(f"Found {len(invalid_gdf)} invalid geometries out of {len(gdf)}. Removing invalid geometries.")
    return valid_gdf


@timer_decorator
def fetch_records(start_date, end_date):
    all_records = []
    start_record = 1
    start_date_str = start_date.strftime('%Y-%m-%d')
    end_date_str = end_date.strftime('%Y-%m-%d')

    while True:
        params = {
            'query': f"(c.product-area==officielepublicaties AND dt.modified>={start_date_str} AND dt.modified<={end_date_str})",
            'startRecord': start_record,
            'maximumRecords': 1000
        }
        response = requests.get(API_ENDPOINT, params=params)
        if response.status_code != 200:
            break

        root = ET.fromstring(response.content)
        records = root.findall('.//sru:record', NAMESPACES)
        if not records:
            break

        for record in records:
            extracted_data = extract_data(record)
            all_records.extend(extracted_data)

        start_record += 1000
        if len(records) < 1000:
            break

    print(f'Records fetched: {len(all_records)}')
    return all_records


def extract_data(record):
    base_data = {field.split(':')[-1]: None for field in FIELDS_TO_EXTRACT}
    for field in FIELDS_TO_EXTRACT:
        found_element = record.find(f'.//{field}', NAMESPACES)
        if found_element is not None:
            base_data[field.split(':')[-1]] = found_element.text

    has_version_element = record.find('.//dcterms:hasVersion', NAMESPACES)
    identifier = base_data.get('identifier')
    source = None
    if has_version_element is not None and 'resourceIdentifier' in has_version_element.attrib:
        source = has_version_element.attrib['resourceIdentifier'].replace('.html', '')

    source_xml = f"https://repository.overheid.nl/sru?&query=(dt.identifier={identifier})" if identifier else None

    record_data = []
    for gebiedsmarkering in record.findall('.//overheidwetgeving:gebiedsmarkering', NAMESPACES):
        geom_type = gebiedsmarkering.tag.split('}')[1]
        geometries = gebiedsmarkering.findall('.//overheidwetgeving:geometrie', NAMESPACES)
        labels = gebiedsmarkering.findall('.//overheidwetgeving:geometrielabel', NAMESPACES)
        for geometry, label in zip(geometries, labels):
            geo_data = base_data.copy()
            geo_data['geometry'] = geometry.text
            geo_data['source'] = source + '.html' if source else None
            geo_data['source_xml'] = source_xml
            geo_data['gebiedsmarkering_type'] = geom_type
            geo_data['geometrieLabel'] = label.text if label is not None else None

            record_data.append(geo_data)
    return record_data


def fetch_metadata_url(source_xml_url, retries=3, backoff_factor=0.3):
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(source_xml_url)
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                metadata_url_element = root.find('.//gzd:itemUrl[@manifestation="metadata"]', NAMESPACES)
                if metadata_url_element is not None:
                    return metadata_url_element.text
                else:
                    print(f"No metadata URL found in source XML for URL: {source_xml_url}")
            else:
                print(f"Failed to fetch source XML from {source_xml_url}, status code: {response.status_code}")
        except Exception as e:
            print(f"Error fetching metadata URL from {source_xml_url}: {e}")
            time.sleep(backoff_factor * (2 ** attempt))  # Exponential backoff
        attempt += 1
    return None


def fetch_referentienummer(metadata_url, retries=3, backoff_factor=0.3):
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(metadata_url)
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                referentienummer_element = root.find('.//metadata[@name="OVERHEIDop.referentienummer"]')
                if referentienummer_element is not None:
                    return referentienummer_element.attrib.get('content')
            else:
                print(f"Failed to fetch metadata from {metadata_url}, status code: {response.status_code}")
        except Exception as e:
            print(f"Error fetching referentienummer from {metadata_url}: {e}")
            time.sleep(backoff_factor * (2 ** attempt))  # Exponential backoff
        attempt += 1
    return None


@timer_decorator
def parse_geometries(df):
    df['geometry'] = df['geometry'].apply(parse_geometry)
    return df


def parse_geometry(geo_str):
    try:
        return wkt.loads(geo_str)
    except Exception as e:
        print("Failed to parse geometry:", geo_str, "Error:", e)
        return None


@timer_decorator
def write_to_postgis(gdf, layer_name, db_url, schema='geo'):
    engine = create_engine(db_url)
    gdf.columns = map(str.lower, gdf.columns)
    print(f"Columns being written to {layer_name}: {gdf.columns}")
    gdf.to_postgis(name=layer_name, con=engine, schema=schema, if_exists='replace', index=False,
                   dtype={'geometry': Geometry(geometry_type='GEOMETRY', srid=28992)})
    print(f'Writing to PostGIS layer: {layer_name}')


def main():
    start_date = start_datum
    end_date = datetime.now()

    records = fetch_records(start_date, end_date)

    df = pd.DataFrame(records)
    df = parse_geometries(df)

    gdf_points = gpd.GeoDataFrame(df[df['geometry'].apply(lambda x: isinstance(x, Point))], geometry='geometry',
                                  crs='EPSG:28992')
    gdf_lines = gpd.GeoDataFrame(df[df['geometry'].apply(lambda x: isinstance(x, (LineString, MultiLineString)))],
                                 geometry='geometry', crs='EPSG:28992')
    gdf_polygons = gpd.GeoDataFrame(df[df['geometry'].apply(lambda x: isinstance(x, (Polygon, MultiPolygon)))],
                                    geometry='geometry', crs='EPSG:28992')

    print(f'Processing PostGIS layers')

    # Validate and clean geometries
    gdf_points = validate_geometries(gdf_points)
    gdf_lines = validate_geometries(gdf_lines)
    gdf_polygons = validate_geometries(gdf_polygons)

    # Filter based on geometry bounds
    gdf_points_within = gdf_points[gdf_points.within(geometry_bounds)]
    gdf_lines_within = gdf_lines[gdf_lines.within(geometry_bounds)]
    gdf_polygons_within = gdf_polygons[gdf_polygons.within(geometry_bounds)]

    # Fetch metadata URL and referentienummer for filtered geometries
    unique_source_xmls = gdf_points_within['source_xml'].dropna().unique().tolist() + \
                         gdf_lines_within['source_xml'].dropna().unique().tolist() + \
                         gdf_polygons_within['source_xml'].dropna().unique().tolist()
    unique_source_xmls = list(set(unique_source_xmls))  # Get unique URLs

    metadata_dict = {}
    referentienummer_dict = {}

    for source_xml in unique_source_xmls:
        metadata_url = fetch_metadata_url(source_xml)
        if metadata_url:
            metadata_dict[source_xml] = metadata_url
            referentienummer_dict[source_xml] = fetch_referentienummer(metadata_url)

    for gdf in [gdf_points_within, gdf_lines_within, gdf_polygons_within]:
        gdf.loc[:, 'metadata_url'] = gdf['source_xml'].map(metadata_dict)
        gdf.loc[:, 'referentienummer'] = gdf['metadata_url'].map(referentienummer_dict)

    # Write to PostGIS
    write_to_postgis(gdf_points_within, layer_point, db_url, schema)
    write_to_postgis(gdf_lines_within, layer_line, db_url, schema)
    write_to_postgis(gdf_polygons_within, layer_polygon, db_url, schema)

    print("Data fetching, saving, processing, and database writing completed successfully.")


if __name__ == '__main__':
    main()
