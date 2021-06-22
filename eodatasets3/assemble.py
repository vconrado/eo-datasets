"""
API for easily writing an ODC Dataset
"""
import shutil
import tempfile
import uuid
import warnings
from copy import deepcopy
from enum import Enum, auto
from pathlib import Path, PurePath
from textwrap import dedent
from typing import Dict, List, Optional, Tuple, Generator, Any, Iterable, Union

import numpy
import rasterio
import xarray
from rasterio import DatasetReader
from rasterio.crs import CRS
from rasterio.enums import Resampling

import eodatasets3
from eodatasets3 import serialise, validate, images, documents
from eodatasets3.documents import find_and_read_documents
from eodatasets3.images import FileWrite, GridSpec, MeasurementRecord
from eodatasets3.model import (
    DatasetDoc,
    ProductDoc,
    Location,
    AccessoryDoc,
)
from eodatasets3.names import NameGenerator, namer
from eodatasets3.properties import Eo3Interface, Eo3Dict
from eodatasets3.validate import Level, ValidationMessage
from eodatasets3.verify import PackageChecksum


class IfExists(Enum):
    """
    Enum: what to do when output already exists?
    """

    #: Skip the dataset
    Skip = auto()
    #: Overwrite the existing dataset
    Overwrite = auto()
    #: Throw an error
    ThrowError = auto()


class AssemblyError(Exception):
    pass


class IncompleteDatasetError(Exception):
    """
    Raised when a dataset is missing essential things and so cannot be written.

    (such as mandatory metadata)
    """

    def __init__(self, validation: ValidationMessage) -> None:
        self.validation = validation


class IncompleteDatasetWarning(UserWarning):
    """A non-critical warning for invalid or incomplete metadata"""

    def __init__(self, validation: ValidationMessage) -> None:
        self.validation = validation

    def __str__(self) -> str:
        return str(self.validation)


class DatasetPreparer(Eo3Interface):

    #: The properties that will automatically be inherited from a source dataset
    #: when :meth:`auto_inherit_properties=True <.add_source_path>`
    #:
    #: These are fields that are inherent to the underlying observation, and so will
    #: still be relevant after most 1:1 processing.
    INHERITABLE_PROPERTIES = {
        "datetime",
        "dtr:end_datetime",
        "dtr:start_datetime",
        "eo:cloud_cover",
        "eo:constellation",
        "eo:gsd",
        "eo:instrument",
        "eo:platform",
        "eo:sun_azimuth",
        "eo:sun_elevation",
        "fmask:clear",
        "fmask:cloud",
        "fmask:cloud_shadow",
        "fmask:snow",
        "fmask:water",
        "gqa:abs_iterative_mean_x",
        "gqa:abs_iterative_mean_xy",
        "gqa:abs_iterative_mean_y",
        "gqa:abs_x",
        "gqa:abs_xy",
        "gqa:abs_y",
        "gqa:cep90",
        "gqa:iterative_mean_x",
        "gqa:iterative_mean_xy",
        "gqa:iterative_mean_y",
        "gqa:iterative_stddev_x",
        "gqa:iterative_stddev_xy",
        "gqa:iterative_stddev_y",
        "gqa:mean_x",
        "gqa:mean_xy",
        "gqa:mean_y",
        "gqa:stddev_x",
        "gqa:stddev_xy",
        "gqa:stddev_y",
        "landsat:collection_category",
        "landsat:collection_number",
        "landsat:landsat_product_id",
        "landsat:landsat_scene_id",
        "landsat:scene_id",
        "landsat:wrs_path",
        "landsat:wrs_row",
        "mission",
        "odc:region_code",
        "sat:absolute_orbit",
        "sat:anx_datetime",
        "sat:orbit_state",
        "sat:platform_international_designator",
        "sat:relative_orbit",
        "sentinel:datastrip_id",
        "sentinel:datatake_start_datetime",
        "sentinel:grid_square",
        "sentinel:latitude_band",
        "sentinel:sentinel_tile_id",
        "sentinel:utm_zone",
    }

    def __init__(
        self,
        collection_location: Optional[Path] = None,
        *,
        dataset_location: Optional[Location] = None,
        metadata_path: Optional[Path] = None,
        dataset_id: Optional[uuid.UUID] = None,
        allow_absolute_paths: bool = False,
        naming_conventions: str = "default",
        names: Optional[NameGenerator] = None,
        dataset: Optional[DatasetDoc] = None,
    ) -> None:
        """
        Creator of EO3 metadata document, with functions for reading information from imagery
        and calculating names and paths.

        In addition to the below documented methods, metadata fields can read and set using
        :class:`Eo3Interface's <eodatasets3.properties.Eo3Interface>` fields.


        There are three optional paths that can be specified. At least one must be specified. Collection,
        dataset or metadata path.

        - A *collection path* is the root folder where datasets will live (in sub-[sub]-folders).
        - Each dataset has its own *dataset location*, as stored in an Open Data Cube index.
          All paths inside the metadata document are relative to this location.
        - An output *metadata document location*.

        If you're writing data, you typically only need to specify the collection path, and the others
        will be automatically generated using the naming conventions.

        If you're only writing a metadata file (for existing data), you only need to specify a metadata path.

        If you're storing data using an exotic URI schema, such as a 'tar://' URL path, you will need to specify
        this as your dataset location.

        :param collection_location:
            Optional base directory where the collection of datasets should live. Subfolders will be
            created accordion to the naming convention.
        :param dataset_location:
            Optional location for this specific dataset. Otherwise it will be generated according to the collection
            path and naming conventions.
        :param metadata_path:
            Optional metadata document output path. Otherwise it will be generated according to the collection path
            and naming conventions.
        :param dataset_id:
            Optional UUID for this dataset, otherwise a random one will be created. Use this if you have a stable
            way of generating your own IDs.
        :param allow_absolute_paths:
            Allow metadata paths to refer to files outside the dataset location. this means they will have to be
            absolute paths, and not be portable. (default: False)
        :param naming_conventions:
            Naming conventions to use. Supports `default` or `dea`. The latter has stricter metadata requirements
            (try it and see -- it will tell your what's missing).
        """

        if not collection_location and not metadata_path:
            raise ValueError(
                "Must specify either a collection folder or a single metadata file"
            )

        if collection_location and not collection_location.exists():
            raise ValueError(
                f"Provided collection location doesn't exist: {collection_location}"
            )

        if not dataset:
            dataset = DatasetDoc()
        if not dataset.id:
            dataset.id = dataset_id or uuid.uuid4()

        self._dataset = dataset

        self._dataset_location = dataset_location
        self._metadata_path = metadata_path

        self._measurements = MeasurementRecord()
        self._accessories: Dict[str, Location] = {}

        self.collection_location = collection_location

        self._allow_absolute_paths = allow_absolute_paths

        self._inherited_geometry = None

        # They may have given us initialised naming conventions already:
        if names is not None:
            #: The name generator  (an instance of :class:`NameGenerator <eodatasets3.NameGenerator>`)
            #:
            #: By default, all names will be generated based on metadata
            #: fields and the chosen naming conventions.
            #:
            #: But you can set your own names here manually to avoid the magic.
            #:
            #: (for the devious among you, this can also avoid metadata field requirements
            #: for name generation).
            #:
            #: Examples:
            #:
            #: Set a product name::
            #:
            #:     p.names.product_name = 'my_product_name'
            #:
            #: Manually set the abbreviations used in name generation
            #:
            #: (By default, for example, landsat-7 will be abbreviated to "ls7". But maybe
            #: you want "ls" in all your datasets)::
            #:
            #:     p.names.platform_abbreviated = "ls"
            #:     # Other abbreviations:
            #:     p.names.instrument_abbreviated = "e"
            #:     p.names.producer_abbreviated = "usgs"
            #:
            #: Set your own label
            #: (the human identifier for the dataset, and the default prefix of filenames)::
            #:
            #:     p.names.dataset_label = "landsat-observations-12th-may-2021"
            #:
            #: Change the folder offset for each dataset. All generated files paths are relative
            #: to this folder (and it is relative to the collection path)::
            #:
            #:     p.names.dataset_folder = Path("datasets/january/2021")
            #:
            #: Set a different pattern used for generating filenames
            #: All filenames have a file_id (eg. "odc-metadata" or "") and a suffix (eg. "yaml")
            #: (Can contain folder separators. It will be relative to the dataset folder)::
            #:
            #:     p.names.file_pattern = "my-file.{file_id}.{suffix}"
            #:
            #: The path to the EO3 metadata doc (relative path to the dataset folder)::
            #:
            #:     p.names.metadata_path = Path("my-metadata.odc-metadata.yaml")
            #:
            #: The URI for the product::
            #:
            #:     p.names.product_uri = "https://collections.earth.test.example/product/my-product"
            #:
            #: A full list of fields can be seen on :class:`eodatasets3.NameGenerator`
            self.names = names
            dataset.properties = names.metadata.properties
        else:
            # Or create some:
            self.names = namer(
                conventions=naming_conventions, properties=dataset.properties
            )

        self._is_completed = False
        self._finished_init_ = True

    @property
    def dataset_id(self) -> uuid.UUID:
        return self._dataset.id

    @dataset_id.setter
    def dataset_id(self, val: Union[uuid.UUID, str]):
        if isinstance(val, str):
            val = uuid.UUID(val)
        self._dataset.id = val

    @property
    def properties(self) -> Eo3Dict:
        return self._dataset.properties

    @property
    def measurements(self) -> Dict[str, Tuple[GridSpec, Path]]:
        return dict(
            (name, (grid, path)) for grid, name, path in self._measurements.iter_paths()
        )

    @property
    def label(self) -> Optional[str]:
        """
        An optional displayable string to identify this dataset.

        These are often used when when presenting a list of datasets, such as in search results or a filesystem folder.
        They are unstructured, but should be more humane than showing a list of UUIDs.

        By convention they have no spaces, due to their usage in filenames.

        Eg. ``ga_ls5t_ard_3-0-0_092084_2009-12-17_final`` or USGS's ``LT05_L1TP_092084_20091217_20161017_01_T1``

        A label will be auto-generated using the naming-conventions, but you can manually override it by
        setting this property.
        """
        return self._dataset.label or self.names.dataset_label

    @label.setter
    def label(self, val: str):
        self._dataset.label = val

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Prevent the accident of setting new properties on the assembler (it has happened multiple times).
        """
        if (
            name != "label"
            and hasattr(self, "_finished_init_")
            and not hasattr(self, name)
        ):
            raise TypeError(
                f"Cannot set new field '{name}' on an assembler. "
                f"(Perhaps you meant to set it on the .properties?)"
            )
        super().__setattr__(name, value)

    def add_source_path(
        self,
        *paths: Path,
        classifier: str = None,
        auto_inherit_properties: bool = False,
        inherit_geometry: bool = False,
    ):
        """
        Record a source dataset using the path to its metadata document.

        :param paths: Filesystem path(s) to source metadata documents

        :param classifier: How to classify the kind of source dataset. This is will automatically
                           be filled with the family of dataset if available (eg. "level1").

                           You want to set this if you have two datasets of the same type that
                           are used for different purposes. Such as having a second level1 dataset
                           that was used for QA (but is not this same scene).
        :param auto_inherit_properties: Whether to copy any common properties from the dataset
        :param inherit_geometry: Instead of re-calculating the valid bounds geometry based on the
                            data, which can be very computationally expensive e.g. Landsat 7
                            striped data, use the valid data geometry from this source dataset.

        See also :meth:`.add_source_dataset`
        """
        for _, doc in find_and_read_documents(*paths):
            # Newer documents declare a schema.
            if "$schema" in doc:
                self.add_source_dataset(
                    serialise.from_doc(doc),
                    classifier=classifier,
                    auto_inherit_properties=auto_inherit_properties,
                    inherit_geometry=inherit_geometry,
                )
            else:
                if auto_inherit_properties or inherit_geometry:
                    raise NotImplementedError(
                        "Can't (yet) inherit properties from old-style metadata"
                    )
                classifier = classifier or doc.get("product_type")
                if not classifier:
                    # TODO: This rule is a little obscure to force people to know.
                    #       We could somehow figure out from the product?
                    raise ValueError(
                        "Source dataset (of old-style eo) doesn't have a 'product_type' property (eg. 'level1', 'fc'), "
                        "you must specify a classifier for the kind of source dataset."
                    )
                self._dataset.lineage.setdefault(classifier, []).append(doc["id"])

    def add_source_dataset(
        self,
        dataset: DatasetDoc,
        classifier: Optional[str] = None,
        auto_inherit_properties: bool = False,
        inherit_geometry: bool = False,
    ):
        """
        Record a source dataset using its metadata document.

        It can optionally copy common properties from the source dataset (platform, instrument etc)/

        (see :py:obj:`.INHERITABLE_PROPERTIES` for the list of fields that are inheritable)

        :param dataset:
        :param auto_inherit_properties: Whether to copy any common properties from the dataset

        :param classifier: How to classify the kind of source dataset. This is will automatically
                           be filled with the family of dataset if available (eg. "level1").

                           You want to set this if you have two datasets of the same type that
                           are used for different purposes. Such as having a second level1 dataset
                           that was used for QA (but is not this same scene).

        :param inherit_geometry: Instead of re-calculating the valid bounds geometry based on the
                            data, which can be very computationally expensive e.g. Landsat 7
                            striped data, use the valid data geometry from this source dataset.

        See :meth:`.add_source_path` if you have a filepath reference instead of a document.

        """

        if not classifier:
            classifier = dataset.properties.get("odc:product_family")
            if not classifier:
                # TODO: This rule is a little obscure to force people to know.
                #       We could somehow figure out the product family from the product?
                raise ValueError(
                    "Source dataset doesn't have a 'odc:product_family' property (eg. 'level1', 'fc'), "
                    "you must specify a classifier for the kind of source dataset."
                )

        self._dataset.lineage.setdefault(classifier, []).append(dataset.id)
        if auto_inherit_properties:
            self._inherit_properties_from(dataset)
        if inherit_geometry:
            self._inherited_geometry = dataset.geometry

    def note_source_datasets(
        self,
        classifier: str,
        *dataset_ids: Union[str, uuid.UUID],
    ):
        """
        Expand the lineage with raw source dataset ids.

        .. note::
           If you have direct access to the datasets, you probably want to use :func:`add_source_path`
           or :func:`add_source_dataset`, so that fields can be inherited from them automatically.

        :param classifier:
                How to classify the source dataset.

                By convention, this is usually the family of the source dataset
                (properties->odc:product_family). Such as "level1".

                A classifier is used to distinguish source datasets of the same product
                that are used differently.

                Such as a normal source level1 dataset (classifier: "level1"), and a
                second source level1 that was used only for QA (classifier: "qa").

        :param dataset_ids: The UUIDs of the source datasets

        """
        for dataset_id in dataset_ids:
            if not isinstance(dataset_id, uuid.UUID):
                try:
                    dataset_id = uuid.UUID(dataset_id)
                except ValueError as v:
                    # The default parse error doesn't tell you anything useful to track down which one broke.
                    raise ValueError(
                        f"Not a valid UUID for source {classifier!r} dataset: {dataset_id!r}"
                    ) from v
            self._dataset.lineage.setdefault(classifier, []).append(dataset_id)

    def _inherit_properties_from(self, source_dataset: DatasetDoc):
        for name in self.INHERITABLE_PROPERTIES:
            if name not in source_dataset.properties:
                continue
            new_value = source_dataset.properties[name]

            try:
                self.properties.normalise_and_set(
                    name,
                    new_value,
                    # If already set, do nothing.
                    allow_override=False,
                )
            except KeyError as k:
                warnings.warn(
                    f"Inheritable property {name!r} is different from current value {k.args}"
                )

    def note_measurement(
        self,
        name,
        path: Location,
        expand_valid_data=True,
        relative_to_dataset_location=False,
    ):
        """
        Reference a measurement from its existing file path.

        (no data is copied, but Geo information is read from it.)

        :param name:
        :param path:
        :param expand_valid_data:
        :param relative_to_dataset_location:
        """
        read_location = path
        if relative_to_dataset_location:
            read_location = documents.resolve_absolute_offset(
                self._dataset_location
                or (self._metadata_path and self._metadata_path.parent),
                path,
            )
        with rasterio.open(read_location) as ds:
            ds: DatasetReader
            if ds.count != 1:
                raise NotImplementedError(
                    "TODO: Only single-band files currently supported"
                )

            self._measurements.record_image(
                name,
                images.GridSpec.from_rio(ds),
                path,
                ds.read(1) if expand_valid_data else None,
                nodata=ds.nodata,
                expand_valid_data=expand_valid_data,
            )

    def dataset_doc(
        self,
        location: Optional[str] = None,
        validate_correctness: bool = True,
        sort_measurements: bool = True,
    ) -> DatasetDoc:
        """
        Get the dataset doc with all pending changes applied.
        """
        location = location or self._metadata_path or self._dataset_location
        if not location:
            if self._metadata_path:
                location = self._metadata_path.parent
            else:
                location = self._dataset_location
            if not location:
                raise ValueError("No location available for metadata paths.")

        dataset = self._dataset
        if not dataset.product:
            dataset.product = ProductDoc()

        dataset.product.name = dataset.product.name or self.names.product_name
        dataset.product.href = dataset.product.href or self.names.product_uri
        dataset.label = dataset.label or self.names.dataset_label

        crs, grid_docs, measurement_docs = self._measurements.as_geo_docs()

        if self._inherited_geometry:
            valid_data = self._inherited_geometry
        else:
            valid_data = self._measurements.consume_and_get_valid_data()
        # Avoid the messiness of different empty collection types.
        # (to have a non-null geometry we'd also need non-null grids and crses)
        if valid_data.is_empty:
            valid_data = None

        new_crs = self._crs_str(crs) if crs is not None else None
        if dataset.crs and dataset.crs != new_crs:
            raise AssemblyError(
                f"New measurements have a different CRS to the underlying dataset. "
                f"Old: {dataset.crs!r}, New: {new_crs!r}"
            )
        dataset.crs = dataset.crs or new_crs
        if valid_data:
            if dataset.geometry:
                dataset.geometry = dataset.geometry.union(valid_data)
            else:
                dataset.geometry = valid_data

        # TODO: this could be made smarter, as we could merge with existing grids.
        #       for now we just throw an error if any of our generated grid names
        #       clash with existing ones.
        if grid_docs:
            if dataset.grids is None:
                dataset.grids = {}

            for name, doc in grid_docs.items():
                if name in dataset.grids:
                    raise NotImplementedError(
                        f"Recorded grid name already exists in the underlying dataset: {name!r},"
                        f"and we don't yet support merging of grids."
                    )
                dataset.grids[name] = doc

        if measurement_docs:
            if dataset.measurements is None:
                dataset.measurements = {}

            for name, doc in measurement_docs.items():
                if name in dataset.measurements:
                    raise AssemblyError(
                        f"Recorded measurement already exists in the underlying dataset: {name!r}"
                    )
                if isinstance(doc.path, PurePath):
                    doc.path = documents.relative_path(
                        doc.path, location.parent
                    ).as_posix()
                dataset.measurements[name] = doc
        for name, path in self._accessories.items():
            if name in dataset.accessories:
                raise AssemblyError(
                    f"Recorded accessory already exists in the underlying dataset: {name!r}"
                )
            if isinstance(path, PurePath):
                path = documents.relative_path(path, location.parent).as_posix()
            dataset.accessories[name] = AccessoryDoc(path, name=name)

        if dataset.measurements and sort_measurements:
            # noinspection PyTypeChecker
            dataset.measurements = dict(sorted(dataset.measurements.items()))

        if validate_correctness:
            doc = serialise.to_doc(dataset)
            for m in validate.validate_dataset(doc):
                if m.level in (Level.info, Level.warning):
                    warnings.warn(IncompleteDatasetWarning(m))
                elif m.level == Level.error:
                    raise IncompleteDatasetError(m)
                else:
                    raise RuntimeError(
                        f"Internal error: Unhandled type of message level: {m.level}"
                    )

        return dataset

    def _crs_str(self, crs: CRS) -> str:
        # TODO: We should support more authorities here.
        #       if rasterio>=1.1.7, can use crs.to_authority(), but almost
        #       everyone is currently on 1.1.6
        return f"epsg:{crs.to_epsg()}" if crs.is_epsg_code else crs.to_wkt()

    def add_accessory_file(self, name: str, path: Location):
        """
        Record a reference to an additional file that's included in the dataset, but is
        not a band/measurement.

        Such as non-ODC metadata, thumbnails, checksums, etc. Any included file that
        is not recorded in the measurements.

        By convention, the name should have prefixes with their category, such as
        ``metadata:`` or ``thumbnail:``.

        eg. ``metadata:landsat_processor``, ``checksum:sha1``, ``thumbnail:full``.

        :param name: identifying name, eg ``metadata:mtl``
        :param path: local path to file.
        """
        existing_path = self._accessories.get(name)
        if existing_path is not None and existing_path != path:
            raise ValueError(
                f"Duplicate accessory name {name!r}. "
                f"New: {path!r}, previous: {existing_path!r}"
            )
        self._accessories[name] = path

    def iter_measurement_paths(
        self,
    ) -> Generator[Tuple[GridSpec, str, Path], None, None]:
        """

        .. warning::
           *not recommended* for use - will likely change soon.

        Iterate through the list of measurement names that have been written, and their current (temporary) paths.

        TODO: Perhaps we want to return a real measurement structure here as it's not very extensible.
        """
        return self._measurements.iter_paths()

    def __str__(self):
        status = "written" if self._is_completed else "unfinished"
        target = (
            self._metadata_path or self._dataset_location or self.collection_location
        )
        measurements = list(self._measurements.iter_names())
        properties = list(self.properties.keys())

        product_name = None
        try:
            product_name = self.names.product_name
        except ValueError:
            ...

        def format_list(items: List, max_len=60):
            s = ", ".join(sorted(items))
            if len(s) > max_len:
                return f"{s[:max_len]}..."
            return s

        return dedent(
            f"""
            Assembling {product_name or ''} ({status})
            - {len(measurements)} measurements: {format_list(measurements)}
            - {len(properties)} properties: {format_list(properties)}
            Writing to {target}
        """
        )

    def __repr__(self):
        return self.__str__()


class DatasetAssembler(DatasetPreparer):
    def __init__(
        self,
        collection_location: Optional[Path] = None,
        *,
        dataset_location: Optional[Location] = None,
        metadata_path: Optional[Path] = None,
        dataset_id: Optional[uuid.UUID] = None,
        # By default, we complain if the output already exists.
        if_exists: IfExists = IfExists.ThrowError,
        allow_absolute_paths: bool = False,
        naming_conventions: str = "default",
        names: Optional[NameGenerator] = None,
        dataset: Optional[DatasetDoc] = None,
    ) -> None:
        """
        Assemble a dataset with ODC metadata, writing metadata and (optionally) its imagery as COGs.

        In addition to the below documented methods, metadata can read and set using
        :class:`Eo3Interface's <eodatasets3.properties.Eo3Interface>` fields.

        There are three optional paths that can be specified. At least one must be specified. Collection,
        dataset or metadata path.

        - A *collection path* is the root folder where datasets will live (in sub-[sub]-folders).
        - Each dataset has its own *dataset location*, as stored in an Open Data Cube index.
          All paths inside the metadata document are relative to this location.
        - An output *metadata document location*.

        If you're writing data, you typically only need to specify the collection path, and the others
        will be automatically generated using the naming conventions.

        If you're only writing a metadata file (for existing data), you only need to specify a metadata path.

        If you're storing data using an exotic URI schema, such as a 'tar://' URL path, you will need to specify
        this as your dataset location.

        :param collection_location:
            Optional base directory where the collection of datasets should live. Subfolders will be
            created accordion to the naming convention.
        :param dataset_location:
            Optional location for this specific dataset. Otherwise it will be generated according to the collection
            path and naming conventions.
        :param metadata_path:
            Optional metadata document output path. Otherwise it will be generated according to the collection path
            and naming conventions.
        :param dataset_id:
            Optional UUID for this dataset, otherwise a random one will be created. Use this if you have a stable
            way of generating your own IDs.
        :param if_exists:
            What to do if the output dataset already exists? By default, throw an error.
        :param allow_absolute_paths:
            Allow metadata paths to refer to files outside the dataset location. this means they will have to be
            absolute paths, and not be portable. (default: False)
        :param naming_conventions:
            Naming conventions to use. Supports `default` or `dea`. The latter has stricter metadata requirements
            (try it and see -- it will tell your what's missing).
        """
        self._exists_behaviour = if_exists
        self._checksum = PackageChecksum()
        self._tmp_work_path: Optional[Path] = None

        self._user_metadata = {}
        self._software_versions: List[Dict] = []

        super().__init__(
            collection_location,
            dataset_location=dataset_location,
            metadata_path=metadata_path,
            dataset_id=dataset_id,
            allow_absolute_paths=allow_absolute_paths,
            naming_conventions=naming_conventions,
            names=names,
            dataset=dataset,
        )

    def _is_writing_files(self):
        """
        Are we writing a package instead of just metadata?
        """
        # A tmpdir is created on the first file written.
        # TODO: support writing files in declared dataset_locations too.
        return self._tmp_work_path is not None

    @property
    def _work_path(self) -> Path:
        """
        Require a folder for writing files into the partially-built dataset.

        The first time this is called, it becomes a packaged dataset rather than a metadata-file-
        writer only.
        """
        if not self._tmp_work_path:
            if not self.collection_location:
                raise ValueError(
                    "Dataset assembler was given no base path on construction: cannot write new files."
                )

            self._tmp_work_path = Path(
                tempfile.mkdtemp(
                    prefix=".odcdataset-", dir=str(self.collection_location)
                )
            )

        return self._tmp_work_path

    def __enter__(self) -> "DatasetAssembler":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Clean up.
        self.close()

    def cancel(self):
        """
        Cancel the package, cleaning up temporary files.

        This works like :meth:`.close`, but is intentional, so no warning will
        be raised for forgetting to complete the package first.
        """
        self._is_completed = True
        self.close()

    def close(self):
        """Clean up any temporary files, even if dataset has not been written"""
        if not self._is_completed:
            warnings.warn(
                "Closing assembler without finishing. "
                "Either call `done()` or `cancel() before closing`"
            )

        if self._tmp_work_path:
            # TODO: add implicit cleanup like tempfile.TemporaryDirectory?
            shutil.rmtree(self._tmp_work_path, ignore_errors=True)

    def write_measurement(
        self,
        name: str,
        input_path: Location,
        overviews: Iterable[int] = images.DEFAULT_OVERVIEWS,
        overview_resampling: Resampling = Resampling.average,
        expand_valid_data: bool = True,
        file_id: str = None,
        path: Path = None,
    ):
        """
        Write a measurement by copying it from a file path.

        Assumes the file is gdal-readable.

        :param name: Identifier for the measurement eg ``'blue'``.
        :param input_path: The image to read
        :param overviews: Set of overview sizes to write
        :param overview_resampling: rasterio Resampling method to use
        :param expand_valid_data: Include this measurement in the valid-data geometry of the metadata.
        :param file_id: Optionally, how to identify this in the filename instead of using the name.
                        (DEA has measurements called ``blue``, but their written filenames must be ``band04`` by
                        convention.)
        :param path: Optional path to the image to write. Can be relative to the dataset.
        """
        with rasterio.open(input_path) as ds:
            self.write_measurement_rio(
                name,
                ds,
                overviews=overviews,
                expand_valid_data=expand_valid_data,
                overview_resampling=overview_resampling,
                file_id=file_id,
                path=path,
            )

    def write_measurement_rio(
        self,
        name: str,
        ds: DatasetReader,
        overviews=images.DEFAULT_OVERVIEWS,
        overview_resampling=Resampling.average,
        expand_valid_data=True,
        file_id=None,
        path: Path = None,
    ):
        """
        Write a measurement by reading it from an open rasterio dataset

        :param ds: An open rasterio dataset

        See :func:`write_measurement` for other parameters.
        """
        if len(ds.indexes) != 1:
            raise NotImplementedError(
                f"TODO: Multi-band images not currently implemented (have {len(ds.indexes)})"
            )

        self._write_measurement(
            name,
            ds.read(1),
            images.GridSpec.from_rio(ds),
            self._work_path
            / (path or self.names.measurement_file(name, "tif", file_id=file_id)),
            expand_valid_data=expand_valid_data,
            nodata=ds.nodata,
            overview_resampling=overview_resampling,
            overviews=overviews,
        )

    def write_measurement_numpy(
        self,
        name: str,
        array: numpy.ndarray,
        grid_spec: GridSpec,
        nodata: Optional[Union[float, int]] = None,
        overviews=images.DEFAULT_OVERVIEWS,
        overview_resampling=Resampling.average,
        expand_valid_data=True,
        file_id: str = None,
        path: Path = None,
    ):
        """
        Write a measurement from a numpy array and grid spec.

        The most common case is to copy the grid spec from your input dataset,
        assuming you haven't reprojected.

        Example::

            p.write_measurement_numpy(
                "blue",
                new_array,
                GridSpec.from_dataset_doc(source_dataset),
                nodata=-999,
            )

        See :func:`write_measurement` for other parameters.

        :param array:
        :param grid_spec:
        :param nodata:
        """
        self._write_measurement(
            name,
            array,
            grid_spec,
            self._work_path
            / (path or self.names.measurement_file(name, "tif", file_id=file_id)),
            expand_valid_data=expand_valid_data,
            nodata=nodata,
            overview_resampling=overview_resampling,
            overviews=overviews,
        )

    def write_measurements_odc_xarray(
        self,
        dataset: xarray.Dataset,
        nodata: Optional[Union[float, int]] = None,
        overviews=images.DEFAULT_OVERVIEWS,
        overview_resampling=Resampling.average,
        expand_valid_data=True,
        file_id=None,
    ):
        """
        Write measurements from an ODC :class:`xarray.Dataset`

        The main requirement is that the Dataset contains a CRS attribute
        and X/Y or lat/long dimensions and coordinates. These are used to
        create an ODC GeoBox.

        :param xarray.Dataset dataset: an xarray dataset (as returned by
                :meth:`datacube.Datacube.load` and other methods)

        See :meth:`write_measurement` for other parameters.
        """
        grid_spec = images.GridSpec.from_odc_xarray(dataset)
        for name, dataarray in dataset.data_vars.items():
            name: str
            self._write_measurement(
                name,
                dataarray.data,
                grid_spec,
                (
                    self._work_path
                    / self.names.measurement_file(name, "tif", file_id=file_id)
                ),
                expand_valid_data=expand_valid_data,
                overview_resampling=overview_resampling,
                overviews=overviews,
                nodata=nodata,
            )

    def _write_measurement(
        self,
        name: str,
        data: numpy.ndarray,
        grid: GridSpec,
        out_path: Path,
        expand_valid_data: bool,
        nodata: Optional[Union[float, int]],
        overview_resampling: Resampling,
        overviews: Tuple[int, ...],
    ):
        res = FileWrite.from_existing(grid.shape).write_from_ndarray(
            data,
            out_path,
            geobox=grid,
            nodata=nodata,
            overview_resampling=overview_resampling,
            overviews=overviews,
        )

        # Ensure the file_format field is set to what we're writing.
        file_format = res.file_format.name
        if "odc:file_format" not in self.properties:
            self.properties["odc:file_format"] = file_format

        if file_format != self.properties["odc:file_format"]:
            raise RuntimeError(
                f"Inconsistent file formats between bands. "
                f"Was {self.properties['odc:file_format']!r}, now {file_format !r}"
            )

        self._measurements.record_image(
            name,
            grid,
            out_path,
            data,
            nodata=nodata,
            expand_valid_data=expand_valid_data,
        )
        # We checksum immediately as the file has *just* been written so it may still
        # be in os/filesystem cache.
        self._checksum.add_file(out_path)

    def write_thumbnail(
        self,
        red: str,
        green: str,
        blue: str,
        resampling: Resampling = Resampling.average,
        static_stretch: Tuple[int, int] = None,
        percentile_stretch: Tuple[int, int] = (2, 98),
        scale_factor: int = 10,
        kind: str = None,
        path: Optional[Path] = None,
    ):
        """
        Write a thumbnail for the dataset using the given measurements (specified by name) as r/g/b.

        (the measurements must already have been written.)

        A linear stretch is performed on the colour. By default this is a dynamic 2% stretch
        (the 2% and 98% percentile values of the input). The static_stretch parameter will
        override this with a static range of values.


        :param red: Name of measurement to put in red band
        :param green: Name of measurement to put in green band
        :param blue: Name of measurement to put in blue band
        :param kind: If you have multiple thumbnails, you can specify the 'kind' name to distinguish
                     them (it will be put in the filename).
                     Eg. GA's ARD has two thumbnails, one of kind ``nbar`` and one of ``nbart``.
        :param scale_factor: How many multiples smaller to make the thumbnail.
        :param percentile_stretch: Upper/lower percentiles to stretch by
        :param resampling: rasterio :class:`rasterio.enums.Resampling` method to use.
        :param static_stretch: Use a static upper/lower value to stretch by instead of dynamic stretch.
        """
        thumb_path = self._work_path / (path or self.names.thumbnail_file(kind=kind))

        missing_measurements = {red, green, blue} - set(self.measurements)
        if missing_measurements:
            raise IncompleteDatasetError(
                ValidationMessage(
                    Level.error,
                    "missing_thumb_measurements",
                    f"Thumbnail measurements are missing: no measurements called {missing_measurements!r}. ",
                    hint=f"Available measurements: {', '.join(self.measurements)}",
                )
            )
        rgbs = [self.measurements[b] for b in (red, green, blue)]
        unique_grids: List[GridSpec] = list(set(grid for grid, path in rgbs))
        if len(unique_grids) != 1:
            raise NotImplementedError(
                "Thumbnails can only currently be created from measurements of the same grid spec."
            )
        grid = unique_grids[0]

        FileWrite().create_thumbnail(
            tuple(path for grid, path in rgbs),
            thumb_path,
            out_scale=scale_factor,
            resampling=resampling,
            static_stretch=static_stretch,
            percentile_stretch=percentile_stretch,
            input_geobox=grid,
        )

        self._document_thumbnail(thumb_path, kind)

    def write_thumbnail_singleband(
        self,
        measurement: str,
        bit: int = None,
        lookup_table: Dict[int, Tuple[int, int, int]] = None,
        kind: str = None,
    ):
        """
        Write a singleband thumbnail out, taking in an input measurement and
        outputting a JPG with appropriate settings.

        :param measurement: Name of measurement
        :param kind: If you have multiple thumbnails, you can specify the 'kind' name to distinguish
                     them (it will be put in the filename).
                     Eg. GA's ARD has two thumbnails, one of kind ``nbar`` and one of ``nbart``.

        EITHER:

        - Use a bit (int) as the value to scale from black to white to
          i.e., 0 will be BLACK and bit will be WHITE, with a linear scale between,::

             p.write_thumbnail_singleband("blue", bit=1)

        OR:

        - Provide a lookup_table (dict) of int (key) [R, G, B] (value) fields
          to make the image with.::

             p.write_thumbnail_singleband(
                 "blue", lookup_table={1: (0, 0, 255)}
             )

        """

        thumb_path = self._work_path / self.names.thumbnail_file(kind=kind)
        _, image_path = self.measurements.get(measurement, (None, None))

        if image_path is None:
            raise IncompleteDatasetError(
                ValidationMessage(
                    Level.error,
                    "missing_thumb_measurement",
                    f"Thumbnail measurement is missing: no measurements called {measurement!r}. ",
                    hint=f"Available measurements: {', '.join(self.measurements)}",
                )
            )

        FileWrite().create_thumbnail_singleband(
            image_path,
            thumb_path,
            bit,
            lookup_table,
        )

        self._document_thumbnail(thumb_path, kind)

    def extend_user_metadata(self, section_name: str, doc: Dict[str, Any]):
        """
        Record extra metadata from the processing of the dataset.

        It can be any document suitable for yaml/json serialisation, and will be written into
        the sidecar "proc-info" metadata.

        This is typically used for recording processing parameters or environment information.

        :param section_name: Should be unique to your product, and identify the kind of document,
                             eg 'brdf_ancillary'
        :param doc: Document
        """
        if section_name in self._user_metadata:
            raise ValueError(f"metadata section {section_name} already exists")

        self._user_metadata[section_name] = deepcopy(doc)

    def note_software_version(self, name: str, url: str, version: str):
        """
        Record the version of some software used to produce the dataset.

        :param name: a short human-readable name for the software. eg "datacube-core"
        :param url: A URL where the software is found, such as the git repository.
        :param version: the version string, eg. "1.0.0b1"
        """
        for v in self._software_versions:
            # Uniquely identify software by the tuple (name, url)
            if v["name"] == name and v["url"] == url:
                existing_version = v["version"]
                if existing_version != version:
                    raise ValueError(
                        f"duplicate setting of software {url!r} with different value "
                        f"({existing_version!r} != {version!r})"
                    )
                return

        self._software_versions.append(dict(name=name, url=url, version=version))

    def _document_thumbnail(self, thumb_path: Path, kind: str = None):
        self._checksum.add_file(thumb_path)

        accessory_name = "thumbnail"
        if kind:
            accessory_name += f":{kind}"
        self.add_accessory_file(accessory_name, thumb_path)

    def _write_yaml(self, doc, path, allow_external_paths=False):
        documents.make_paths_relative(
            doc, path.parent, allow_paths_outside_base=allow_external_paths
        )
        serialise.dump_yaml(path, doc)
        self._checksum.add_file(path)

    def done(
        self, validate_correctness: bool = True, sort_measurements: bool = True
    ) -> Tuple[uuid.UUID, Path]:
        """
        Write the dataset and move it into place.

        It will be validated, metadata will be written, and if all is correct, it will be
        moved to the output location.

        The final move is done atomically, so the dataset will only exist in the output
        location if it is complete.

        :param validate_correctness: Run the eo3-validator on the resulting metadata.
        :param sort_measurements: Order measurements alphabetically. (instead of insert-order)
        :raises: :class:`IncompleteDatasetError` If any critical metadata is incomplete.

        :returns: The id and final path to the dataset metadata file.
        """
        self.note_software_version(
            "eodatasets3",
            "https://github.com/GeoscienceAustralia/eo-datasets",
            eodatasets3.__version__,
        )

        metadata_path = (
            self._metadata_path
            or self._work_path / self.names.metadata_file(suffix="odc-metadata.yaml")
        )

        # Are we writing a package instead of just metadata?
        if self._is_writing_files():
            # (the checksum isn't written yet -- it'll be the last file)
            self.add_accessory_file(
                "checksum:sha1", self._work_path / self.names.checksum_file()
            )

            processing_metadata = self._work_path / self.names.metadata_file(
                suffix="proc-info.yaml"
            )
            self._write_yaml(
                {**self._user_metadata, "software_versions": self._software_versions},
                self._work_path / processing_metadata,
                allow_external_paths=True,
            )
            self.add_accessory_file("metadata:processor", processing_metadata)

        dataset = self.dataset_doc(
            location=metadata_path,
            validate_correctness=validate_correctness,
            sort_measurements=sort_measurements,
        )
        self._write_yaml(
            serialise.to_formatted_doc(dataset),
            metadata_path,
        )

        # If we're writing data, not just a metadata file, finish the package and move it into place.
        if self._is_writing_files():
            self._checksum.write(
                self._work_path / dataset.accessories["checksum:sha1"].path
            )

            # Match the lower r/w permission bits to the output folder.
            # (Temp directories default to 700 otherwise.)
            self._work_path.chmod(self.collection_location.stat().st_mode & 0o777)

            # GDAL writes extra metadata in aux files,
            # but we consider it a mistake if you're using those extensions.
            for aux_file in self._work_path.rglob("*.aux.xml"):
                warnings.warn(
                    f"Cleaning unexpected gdal aux file {aux_file.as_posix()!r}"
                )
                aux_file.unlink()

            if not self._dataset_location:
                self._dataset_location = (
                    self.collection_location / self.names.dataset_folder
                )
            # Now atomically move to final location.
            # Someone else may have created the output while we were working.
            # Try, and then decide how to handle it if so.
            try:
                self._dataset_location.parent.mkdir(parents=True, exist_ok=True)
                self._work_path.rename(self._dataset_location)
            except OSError:
                if not self._dataset_location.exists():
                    # Some other error?
                    raise

                if self._exists_behaviour == IfExists.Skip:
                    # Something else created it while we were busy.
                    warnings.warn(f"Skipping -- exists: {self.names.dataset_folder}")
                elif self._exists_behaviour == IfExists.ThrowError:
                    raise
                elif self._exists_behaviour == IfExists.Overwrite:
                    raise NotImplementedError("overwriting outputs not yet implemented")
                else:
                    raise RuntimeError(
                        f"Unexpected exists behaviour: {self._exists_behaviour}"
                    )

        target_metadata_path = (
            self._metadata_path
            or self._dataset_location
            / self.names.metadata_file(suffix="odc-metadata.yaml")
        )
        assert target_metadata_path.exists()
        self._is_completed = True
        return dataset.id, target_metadata_path
