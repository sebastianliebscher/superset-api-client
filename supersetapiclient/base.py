"""Base classes."""
import logging
import dataclasses
try:
    from functools import cached_property
except ImportError:
    # Python<3.8
    from cached_property import cached_property
import json
from typing import List, Union
from pathlib import Path

import os.path
import yaml
from requests import Response, HTTPError

from supersetapiclient.exceptions import ComplexBadRequestError, NotFound, BadRequestError

logger = logging.getLogger(__name__)


def json_field():
    return dataclasses.field(default=None, repr=False)


def default_string():
    return dataclasses.field(default="", repr=False)


def raise_for_status(response):
    try:
        response.raise_for_status()
    except HTTPError as e:
        # Attempt to propagate the server error message
        try:
            error_msg = response.json()["message"]
        except Exception:
            try:
                errors = response.json()["errors"]
            except Exception:
                raise e
            raise ComplexBadRequestError(*e.args, request=e.request, response=e.response, errors=errors) from None
        raise BadRequestError(*e.args, request=e.request, response=e.response, message=error_msg) from None


class Object:
    _parent = None
    EXPORTABLE = False
    JSON_FIELDS = []

    @classmethod
    def fields(cls):
        """Get field names."""
        return dataclasses.fields(cls)

    @classmethod
    def field_names(cls):
        """Get field names."""
        return set(
            f.name
            for f in dataclasses.fields(cls)
        )

    @classmethod
    def from_json(cls, json: dict):
        """Create Object from json

        Args:
            json (dict): a dictionary

        Returns:
            Object: return the related object
        """
        field_names = cls.field_names()
        return cls(**{k: v for k, v in json.items() if k in field_names})

    def to_json(self, columns):
        o = {}
        for c in columns:
            if not hasattr(self, c):
                # Column that is not implemented yet
                continue
            value = getattr(self, c)
            if c in self.JSON_FIELDS:
                value = json.dumps(value)
            o[c] = value
        return o

    def __post_init__(self):
        for f in self.JSON_FIELDS:
            setattr(self, f, json.loads(getattr(self, f) or "{}"))

    @property
    def base_url(self) -> str:
        return self._parent.client.join_urls(
            self._parent.base_url,
            str(self.id)
        )

    @property
    def import_url(self) -> str:
        return self._parent.client.join_urls(
            self._parent.base_url,
            str(self.id)
        )

    @property
    def export_url(self) -> str:
        """Export url for a single object."""
        # Note that params should be passed on run
        # to bind to a specific object
        return self._parent.client.join_urls(
            self.base_url,
            "export"
        )

    @property
    def test_connection_url(self) -> str:
        return self._parent.client.join_urls(
            self._parent.base_url,
            str(self.id)
        )

    def export(self, path: Union[Path, str]) -> None:
        """Export object to path"""
        if not self.EXPORTABLE:
            raise NotImplementedError(
                "Export is not defined for this object."
            )

        # Get export response
        client = self._parent.client
        response = client.get(self.export_url, params={
            "q": [self.id]  # Object must have an id field to be exported
        })
        raise_for_status(response)

        with open(path, "w", encoding="utf-8") as f:
            f.write(response.text)

    def fetch(self) -> None:
        """Fetch additional object information."""
        field_names = self.field_names()

        client = self._parent.client
        reponse = client.get(self.base_url)
        o = reponse.json()
        o = o.get("result")
        for k, v in o.items():
            if k in field_names:
                setattr(self, k, v)

    def save(self) -> None:
        """Save object information."""
        o = self.to_json(columns=self._parent.edit_columns)
        response = self._parent.client.put(self.base_url, json=o)
        if response.status_code in [400, 422]:
            logger.error(response.text)
        raise_for_status(response)

    def delete(self) -> bool:
        return self._parent.delete(id=self.id)


class ObjectFactories:
    endpoint = ""
    base_object: Object = None

    _INFO_QUERY = {
        "keys": [
            "add_columns",
            "edit_columns"
        ]
    }

    def __init__(self, client):
        """Create a new Dashboards endpoint.

        Args:
            client (client): superset client
        """
        self.client = client

    @cached_property
    def _infos(self):
        # Get infos
        response = self.client.get(
            self.client.join_urls(
                self.base_url,
                "_info",
            ),
            params={
                "q": json.dumps(self._INFO_QUERY)
            })

        if response.status_code != 200:
            logger.error(f"Unable to build object factory for {self.endpoint}")
            raise_for_status(response)

        return response.json()

    @property
    def add_columns(self):
        return [
            e.get("name")
            for e in self._infos.get("add_columns", [])
        ]

    @property
    def edit_columns(self):
        return [
            e.get("name")
            for e in self._infos.get("edit_columns", [])
        ]

    @property
    def base_url(self):
        """Base url for these objects."""
        return self.client.join_urls(
            self.client.base_url,
            self.endpoint,
        )

    @property
    def import_url(self):
        """Base url for these objects."""
        return self.client.join_urls(
            self.client.base_url,
            self.endpoint,
            "import/"
        )

    @property
    def export_url(self):
        """Base url for these objects."""
        return self.client.join_urls(
            self.client.base_url,
            self.endpoint,
            "export/"
        )

    @property
    def test_connection_url(self):
        """Base url for these objects."""
        return self.client.join_urls(
            self.client.base_url,
            self.endpoint,
            "test_connection"
        )

    @staticmethod
    def _handle_reponse_status(response: Response) -> None:
        """Handle response status."""
        if response.status_code not in (200, 201):
            logger.error(
                f"Unable to proceed, API return {response.status_code}"
            )
            logger.error(f"Full API response is {response.text}")

        # Finally raising for status
        raise_for_status(response)

    def get(self, id: int):
        """Get an object by id."""
        url = self.base_url + str(id)
        response = self.client.get(
            url
        )
        raise_for_status(response)
        response = response.json()

        object_json = response.get("result")
        object_json["id"] = id
        object = self.base_object.from_json(object_json)
        object._parent = self

        return object

    def find(self, page_size: int = 100, page: int = 0, **kwargs):
        """Find and get objects from api."""
        url = self.base_url

        # Get response
        query = {
            "page_size": page_size,
            "page": page,
            "filters": [
                {
                    "col": k,
                    "opr": "eq",
                    "value": v
                } for k, v in kwargs.items()
            ]
        }

        params = {
            "q": json.dumps(query)
        }

        response = self.client.get(
            url,
            params=params
        )
        raise_for_status(response)
        response = response.json()

        objects = []
        for r in response.get("result"):
            o = self.base_object.from_json(r)
            o._parent = self
            objects.append(o)

        return objects

    def count(self):
        """Count objects."""

        # To do : add kwargs parameters for more flexibility
        response = self.client.get(self.base_url)

        if response.status_code not in (200, 201):
            logger.error(response.text)
        raise_for_status(response)

        json = response.json()
        return (json['count'])

    def find_one(self, **kwargs):
        """Find only object or raise an Exception."""
        objects = self.find(**kwargs)
        if len(objects) == 0:
            raise NotFound(f"No {self.base_object.__name__} has been found.")
        return objects[0]

    def add(self, obj) -> int:
        """Create a object on remote."""

        o = obj.to_json(columns=self.add_columns)
        response = self.client.post(self.base_url, json=o)
        raise_for_status(response)
        obj.id = response.json().get("id")
        obj._parent = self
        return obj.id

    def export(self, ids: List[int], path: Union[Path, str]) -> None:
        """Export object into an importable file"""
        client = self.client
        url = self.export_url
        ids_array = ','.join([str(i) for i in ids])
        response = client.get(
            url,
            params={
                "q": f"[{ids_array}]"
            })

        if response.status_code not in (200, 201):
            logger.error(response.text)
        raise_for_status(response)

        content_type = response.headers["content-type"].strip()
        if content_type.startswith("application/text"):
            data = response.text
            data = yaml.load(data, Loader=yaml.FullLoader)
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False)
        if content_type.startswith("application/zip"):
            data = response.content
            with open(path, 'wb') as f:
                f.write(data)
        else:
            data = response.json()
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

        return data

    def delete(self, id: int) -> bool:
        """Delete a object on remote."""
        response = self.client.delete(self.base_url + str(id))

        if response.status_code not in (200, 201):
            logger.error(response.text)
        raise_for_status(response)

        if response.json().get('message') == 'OK':
            return True
        else:
            return False

    def import_file(self, file_path, overwrite=False, passwords=None) -> dict:
        """Import a file on remote.

        :param file_path: Path to a JSON or ZIP file containing the import data
        :param overwrite: If True, overwrite existing remote entities
        :param passwords: JSON map of passwords for each featured database in
        the file. If the ZIP includes a database config in the path
        databases/MyDatabase.yaml, the password should be provided in the
        following format: {"MyDatabase": "my_password"}
        """
        url = self.import_url

        data = {"overwrite": json.dumps(overwrite)}
        passwords = {
            f"databases/{db}.yaml": pwd for db, pwd in (passwords or {}).items()
        }
        file_name = os.path.split(file_path)[-1]
        file_ext = os.path.splitext(file_name)[-1].lstrip(".").lower()
        with open(file_path, "rb") as f:
            files = {
                "formData": (file_name, f, f"application/{file_ext}"),
                "passwords": (None, json.dumps(passwords), None),
            }
            response = self.client.post(
                url, files=files, data=data,
                headers={"Accept": "application/json"}
            )
        raise_for_status(response)

        # If import is successful, the following is returned: {'message': 'OK'}
        return response.json()

    def test_connection(self, obj):
        """Import a file on remote."""
        url = self.test_connection_url
        connection_columns = ['database_name', 'sqlalchemy_uri']
        o = {}
        for c in connection_columns:
            if hasattr(obj, c):
                value = getattr(obj, c)
                o[c] = value

        response = self.client.post(url, json=o)
        if response.json().get('message') == 'OK':
            return True
        else:
            return False
