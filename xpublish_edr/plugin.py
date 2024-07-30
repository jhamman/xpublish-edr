"""
OGC EDR router for datasets with CF convention metadata
"""
import asyncio
import logging
from functools import cache
from typing import Hashable, List, Optional, Tuple

import cachey
import dask
import pkg_resources
import xarray as xr
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from xpublish import Dependencies, Plugin, hookimpl
from xpublish.utils.cache import CostTimer

from .formats.to_covjson import to_cf_covjson
from .query import EDRQuery, edr_query, edr_query_params

logger = logging.getLogger("cf_edr")


def cache_key_from_request(
    route: str,
    request: Request,
    query: EDRQuery,
    dataset: xr.Dataset,
) -> Tuple[Hashable, ...]:
    """Generate a cache key from the request and query parameters"""
    return (route, request, query, dask.base.tokenize(dataset))


@cache
def position_formats():
    """
    Return response format functions from registered
    `xpublish_edr_position_formats` entry_points
    """
    formats = {}

    for entry_point in pkg_resources.iter_entry_points("xpublish_edr_position_formats"):
        formats[entry_point.name] = entry_point.load()

    return formats


class CfEdrPlugin(Plugin):
    """
    OGC EDR compatible endpoints for Xpublish datasets
    """

    name: str = "cf_edr"

    app_router_prefix: str = "/edr"
    app_router_tags: List[str] = ["edr"]

    dataset_router_prefix: str = "/edr"
    dataset_router_tags: List[str] = ["edr"]

    @hookimpl
    def app_router(self):
        """Register an application level router for EDR format info"""
        router = APIRouter(prefix=self.app_router_prefix, tags=self.app_router_tags)

        @router.get(
            "/position/formats",
            summary="Position query response formats",
        )
        async def get_position_formats():
            """
            Returns the various supported formats for position queries
            """
            formats = {key: value.__doc__ for key, value in position_formats().items()}

            return formats

        return router

    @hookimpl
    def dataset_router(self, deps: Dependencies):
        """Register dataset level router for EDR endpoints"""
        router = APIRouter(prefix=self.app_router_prefix, tags=self.dataset_router_tags)

        @router.get("/position", summary="Position query")
        async def get_position(
            request: Request,
            query: EDRQuery = Depends(edr_query),
            dataset: xr.Dataset = Depends(deps.dataset),
            cache: cachey.Cache = Depends(deps.cache),
        ):
            """
            Returns position data based on WKT `Point(lon lat)` coordinates

            Extra selecting/slicing parameters can be provided as extra query parameters
            """
            cache_key = cache_key_from_request("position", request, query, dataset)
            response: Optional[Response] = cache.get(cache_key)

            if response is not None:
                logger.debug(f"Cache hit for {cache_key}")
                return response

            with CostTimer() as ct:
                try:
                    ds = dataset.cf.sel(
                        X=query.point.x,
                        Y=query.point.y,
                        method="nearest",
                    )
                except KeyError:
                    raise HTTPException(
                        status_code=404,
                        detail="Dataset does not have CF Convention compliant metadata",
                    )

                if query.z:
                    ds = dataset.cf.sel(Z=query.z, method="nearest")

                if query.datetime:
                    datetimes = query.datetime.split("/")

                    try:
                        if len(datetimes) == 1:
                            ds = ds.cf.sel(T=datetimes[0], method="nearest")
                        elif len(datetimes) == 2:
                            ds = ds.cf.sel(T=slice(datetimes[0], datetimes[1]))
                        else:
                            raise HTTPException(
                                status_code=404,
                                detail="Invalid datetimes submitted",
                            )
                    except ValueError as e:
                        logger.error("Error with datetime", exc_info=True)
                        raise HTTPException(
                            status_code=404,
                            detail=f"Invalid datetime ({e})",
                        ) from e

                if query.parameters:
                    try:
                        ds = ds.cf[query.parameters.split(",")]
                    except KeyError as e:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Invalid variable: {e}",
                        )

                    logger.debug(f"Dataset filtered by query params {ds}")

                query_params = dict(request.query_params)
                for query_param in request.query_params:
                    if query_param in edr_query_params:
                        del query_params[query_param]

                method: Optional[str] = "nearest"

                for key, value in query_params.items():
                    split_value = value.split("/")
                    if len(split_value) == 1:
                        continue
                    elif len(split_value) == 2:
                        query_params[key] = slice(split_value[0], split_value[1])
                        method = None
                    else:
                        raise HTTPException(
                            404,
                            f"Too many values for selecting {key}",
                        )

                ds = ds.sel(query_params, method=method)

                if query.format:
                    try:
                        format_fn = position_formats()[query.format]
                    except KeyError:
                        raise HTTPException(
                            404,
                            f"{query.format} is not a valid format for EDR position queries. "
                            "Get `./formats` for valid formats",
                        )
                else:
                    format_fn = to_cf_covjson

            response = await asyncio.to_thread(format_fn, ds)
            assert isinstance(response, Response)  # TODO: make mypy happy another way

            cache.put(
                cache_key,
                response,
                ct.time,
                int(response.headers["content-length"]),
            )
            return response

        return router
