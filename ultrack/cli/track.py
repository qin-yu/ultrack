from typing import Optional

import click

from ultrack import track
from ultrack.cli.utils import batch_index_option, config_option
from ultrack.config import MainConfig


@click.command("track")
@config_option()
@batch_index_option()
def track_cli(config: MainConfig, batch_index: Optional[int]) -> None:
    """Compute tracks by selecting optimal segmentation candidates and links."""
    track(config.tracking_config, config.data_config, batch_index)