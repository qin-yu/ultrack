import logging
from contextlib import nullcontext
from typing import Optional

import fasteners
import numpy as np
import pandas as pd
import sqlalchemy as sqla
from scipy.spatial import KDTree
from sqlalchemy.orm import Session
from toolz import curry

from ultrack.config import DataConfig, LinkingConfig
from ultrack.core.database import LinkDB, NodeDB, maximum_time
from ultrack.core.linking.utils import clear_linking_data
from ultrack.utils.multiprocessing import (
    multiprocessing_apply,
    multiprocessing_sqlite_lock,
)

logging.basicConfig()
logging.getLogger("sqlachemy.engine").setLevel(logging.INFO)

LOG = logging.getLogger(__name__)


@curry
def _process(
    time: int,
    config: LinkingConfig,
    db_path: str,
    write_lock: Optional[fasteners.InterProcessLock] = None,
) -> None:
    """Link nodes from current time to time + 1.

    Parameters
    ----------
    time : int
        Current time.
    config : LinkingConfig
        Linking configuration parameters.
    db_path : str
        Database path.
    write_lock : Optional[fasteners.InterProcessLock], optional
        Lock object for SQLite multiprocessing, optional otherwise, by default None.
    """
    engine = sqla.create_engine(db_path)
    with Session(engine) as session:
        query = session.query(NodeDB.pickle)

        current_nodes = [n for n, in query.where(NodeDB.t == time)]
        next_nodes = [n for n, in query.where(NodeDB.t == time + 1)]

    current_pos = np.asarray([n.centroid for n in current_nodes])
    next_pos = np.asarray([n.centroid for n in next_nodes])

    # finds neighbors nodes within the radius
    # and connect the pairs with highest IoU
    current_kdtree = KDTree(current_pos)
    next_kdtree = KDTree(next_pos)

    neighbors = current_kdtree.query_ball_tree(
        next_kdtree,
        r=config.max_distance,
    )

    links = []
    for i, node in enumerate(current_nodes):
        neighborhood = []
        neigh_size = len(neighbors[i])
        for j, neigh_idx in enumerate(neighbors[i]):
            neigh = next_nodes[neigh_idx]
            iou = node.IoU(neigh)
            # assuming neighbors are ordered so size - j will be used as tie breaker
            neighborhood.append((iou, neigh_size - j, node.id, neigh.id))

        neighborhood = sorted(neighborhood, reverse=True)[: config.max_neighbors]
        LOG.info(f"Node {node.id} links {neighborhood}")
        links += neighborhood

    links = np.asarray(links)[:, [0, 2, 3]]  # ignoring index column
    df = pd.DataFrame(links, columns=["iou", "source_id", "target_id"])

    with write_lock if write_lock is not None else nullcontext():
        LOG.info(f"Pushing links from time {time} to {db_path}")
        engine = sqla.create_engine(db_path, hide_parameters=True)
        with engine.begin() as conn:
            df.to_sql(
                name=LinkDB.__tablename__, con=conn, if_exists="append", index=False
            )


def link(
    linking_config: LinkingConfig,
    data_config: DataConfig,
    overwrite: bool = False,
) -> None:
    """Links candidate segments (nodes) with their neighbors on the next time.

    Parameters
    ----------
    linking_config : LinkingConfig
        Linking configuration parameters.
    data_config : DataConfig
        Data configuration parameters.
    overwrite : bool
        Cleans up linking database content before processing.
    """
    LOG.info(f"Linking nodes with LinkingConfig:\n{linking_config}")

    max_t = maximum_time(data_config)

    if overwrite:
        clear_linking_data(data_config.database_path)

    with multiprocessing_sqlite_lock(data_config) as lock:
        process = _process(
            config=linking_config,
            db_path=data_config.database_path,
            write_lock=lock,
        )
        multiprocessing_apply(
            process, range(max_t), linking_config.n_workers, desc="Linking nodes."
        )
