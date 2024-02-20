from base64 import b32encode
from collections.abc import MutableMapping
from hashlib import sha256
from os import listdir
import sys
from typing import Any


import subprocess as sp

import logging
import shutil
import tempfile as tmp
from pathlib import Path
import click


from st_hsdatalog.HSD.HSDatalog import (
    HSDatalog as HSDatalogFactory,
    HSDatalog_v2,
)
import ulid
from vic.acquisitioninfo import AcquisitionInfo, TagRange
from vic.deviceconfig import DeviceConfig

from vic.models import (
    Component,
    DataItem,
    Device,
    Source,
    VespucciInertialCsvDataset,
)

PACKAGE_NAME = "vic"

DIGEST_READ_BUFFER_LENGTH = 512 * 2**4
FILENAME_DIGEST_TRUNCATION_LENGTH = 8

log = logging.getLogger(PACKAGE_NAME)


@click.command()
@click.argument("input-dir")
@click.option("--output-dir")
@click.option("--log-level", default="INFO")
def run(input_dir: str, output_dir: str, log_level: str) -> None:
    logging.basicConfig(level=log_level.upper())

    input_path = Path(input_dir).absolute()

    if not input_path.exists():
        raise Exception(f"Input dir does not exist: {input_path}")

    if not input_path.is_dir():
        raise Exception(
            f"Input dir exists but is not a directory: {input_path}"
        )

    output_path = Path(output_dir).absolute()

    if not output_path.exists():
        if not output_path.parent.exists():
            raise Exception(
                "Output dir does not exist and neither does the parent"
            )

        log.debug(
            "Output folder %s does not exist, it will created", output_path
        )
    else:
        if not output_path.is_dir():
            raise Exception(
                f"Output dir exists but is not a directory: {input_path}"
            )

        log.debug("Output folder %s exists, no need to create it")

    with (
        tmp.TemporaryDirectory() as temp_dir_in,
        tmp.TemporaryDirectory() as temp_dir_out,
    ):
        temp_path_in = Path(temp_dir_in).absolute()

        assert temp_path_in.exists() and temp_path_in.is_dir()
        log.debug("Created temp input dir: %s", temp_path_in)

        temp_path_out = Path(temp_dir_out).absolute()

        assert temp_path_out.exists() and temp_path_out.is_dir()
        log.debug("Created temp output dir: %s", temp_path_out)

        log.debug("Copying input folder to temp path")

        _ = shutil.copytree(input_path, temp_path_in, dirs_exist_ok=True)

        dat_to_csv(temp_path_in, temp_path_out)

        assemble_metadata(temp_path_in, temp_path_out)

        _ = shutil.copytree(temp_path_out, output_path, dirs_exist_ok=True)

    log.debug("All done")


def assemble_metadata(temp_path_in: Path, temp_path_out: Path) -> None:

    log.debug("Assembling dataset metadata")

    hsd: HSDatalog_v2 = HSDatalogFactory().create_hsd(
        acquisition_folder=str(temp_path_in)
    )

    acq_info = hsd.get_acquisition_info()

    acq_info_validated = AcquisitionInfo.model_validate(acq_info)

    source = Source.model_validate({"blob_id": "none", "metadata": acq_info})

    log.debug("Datalog source info: %s", source)

    device_config_valid = DeviceConfig.model_validate(hsd.get_device())

    device_config_components = device_config_valid.get_components()

    components_valid = list(
        Component.from_device_config_component(device_config_component)
        for device_config_component in device_config_components
    )

    device: MutableMapping[str, Any] = {
        "components": components_valid,
        "board_id": device_config_valid.board_id,
        "fw_id": device_config_valid.fw_id,
        "metadata": {},
    }

    device_valid = Device.model_validate(device)

    log.debug("Component metadata:\n%s", device_config_valid)

    tags = set(item["Label"] for item in hsd.get_time_tags())

    subfolders = tuple(
        temp_path_out.joinpath(item) for item in listdir(temp_path_out)
    )

    assert all(item.exists() and item.is_dir() for item in subfolders)
    assert set(item.name for item in subfolders) == tags

    log.debug("Detected tag set: %s", tags)

    data_items = []

    tag_ranges = TagRange.from_tag_events(acq_info_validated.tag_events)

    log.debug("Computed tag ranges: %s", tag_ranges)

    for subfolder in subfolders:

        one_tag_ranges = tuple(
            tag_range
            for tag_range in tag_ranges
            if tag_range.label == subfolder.name
        )

        log.debug("Processing subfolder %s", subfolder)

        files = tuple(subfolder.joinpath(item) for item in listdir(subfolder))

        assert all(file.name.endswith(".csv") for file in files)
        assert len(files) == len(one_tag_ranges)

        for tag_range, file in zip(one_tag_ranges, files):

            digest = sha256()

            with open(file, "rb") as file_handle:
                while (
                    read_bytes := file_handle.read(DIGEST_READ_BUFFER_LENGTH)
                ) != b"":
                    digest.update(read_bytes)

            file_id = b32encode(digest.digest()).decode("utf-8").lower()

            # Length of a 32 byte string encoded in base32 (4 bytes of padding).
            assert len(file_id) == 56
            assert file_id.endswith("====")

            ext = "csv"

            file_name = f"{file_id[:8]}.{ext}"

            file.rename(file.with_name(file_name))

            file_item = {
                "id": file_id,
                "relative_path": f"{subfolder.name}/{file_name}",
                "type": "text/csv",
                "extension": ext,
            }

            data_item = {
                "class": tag_range.label,
                "start_time": tag_range.start_time,
                "end_time": tag_range.end_time,
                "file": file_item,
                "source": source,
                "device": device_valid,
            }

            data_item_valid = DataItem.model_validate(data_item)
            data_items.append(data_item_valid)

    dataset = VespucciInertialCsvDataset(
        name="my converted dataset",
        description="My dataset description",
        id=str(ulid.ULID()).lower(),
        classes=list(tags),
        data=data_items,
        metadata={},
    )

    with open(temp_path_out.joinpath("dataset-meta.json"), "wt") as file_handle:
        file_handle.write(dataset.model_dump_json(indent=2))


def dat_to_csv(temp_path_in: Path, temp_path_out: Path) -> None:

    log.info("Starting conversion program...")

    _process = sp.run(
        (
            "python",
            str(Path(__file__).parent.joinpath("hsdatalog_to_unico.py")),
            "-s",
            "all",
            "-t",
            "-f",
            "CSV",
            "-o",
            str(temp_path_out),
            str(temp_path_in),
        ),
        stdout=sys.stdout,
        stderr=sys.stderr,
        check=True,
    )

    log.info("Conversion from `.dat` succeeded")
