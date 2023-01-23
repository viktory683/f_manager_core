import json
from typing import Generator
import urllib.parse as urlparse
import zipfile
from functools import reduce

import requests
from bs4 import BeautifulSoup

from . import config, exceptions
from .logger import logger


class Mod:
    """
    A class to represent a mod.

    ...

    Attributes
    ----------
    name : str
        name of the mod
    enabled : bool
        special info for the profile
    downloaded : bool
        is mod exists in the mods folder
    version : str
        version of the mod
    dependencies : dict[str, list[Mod]]
        mod dependencies
        dict format is
        {
            "require": [],
            "optional": [],
            'conflict': [],
            "parent": []
        }
    downloaded_mods : Generator[Mod]
        Check for list of downloaded mods

    Methods
    -------
    update():
        Check for the new version of the mod.
    upgrade():
        Updates the version of the mod and downloads it
    remove():
        Removes the mod with his dependencies
    """

    # TODO refactor and reformat
    # TODO add if mod is not dependents of 'base' but dependents on factorio version
    def __init__(self, name, enabled=True):
        """
        Constructs all the necessary attributes for the mod object.

        Parameters
        ----------
            name : str
                name of the mod
            enabled : bool, optional
                special info for the profile
        """
        self.name = name
        self.enabled = enabled
        self._downloaded = None
        self._version = None
        self._has_new_version = None
        self._dependencies = {
            "require": [],
            "optional": [],
            'conflict': [],
            "parent": []
        }

    @property
    def downloaded(self):
        if self._downloaded:
            return self._downloaded

        self._downloaded = self.name in map(
            lambda file: file.stem.rsplit("_", 1)[0],
            config.mods_file.parent.rglob('*.zip')
        ) if self.name != "base" else True

        return self._downloaded

    @property
    def version(self):
        if not self.downloaded:
            return None

        if self._version:
            return self._version

        if self.name == "base":
            with (
                config.game_folder.
                joinpath("data").
                joinpath("base").
                joinpath("info.json")
            ).open() as f:
                self._version = json.load(f)["version"]
        else:
            for filename in config.mods_file.parent.rglob("*.zip"):
                name, version = filename.stem.rsplit("_", 1)
                if name == self.name:
                    self._version = version
                    break

        return self._version

    def _parse_dependency(
        self,
        dependency_raw,
        categories: dict[str, list[str] | None]
    ):
        # categories format
        # {
        #     "category": ["pref1", "pref2", ...],
        #     ...,
        #     "default_category": None  # for "require" or what
        # }
        if any(
            map(
                lambda prefix: dependency_raw.startswith(prefix),
                reduce(
                    lambda a, b: a + b,
                    filter(lambda v: v, categories.values())
                )
            )
        ):
            for category, prefixes in categories.items():
                if any(
                    map(
                        lambda prefix: dependency_raw.startswith(prefix),
                        prefixes
                    )
                ):
                    dependency = dependency_raw
                    for prefix in prefixes:
                        dependency = dependency.strip(prefix)
                    dependency = dependency.strip()
                    dependency = dependency.split()[0]
                    return category, dependency
        else:
            dependency = dependency_raw
            dependency = dependency.strip()
            dependency = dependency.split()[0]

            for category, prefixes in categories.items():
                if prefixes is None:
                    return category, dependency

    @property
    def dependencies(self):
        # TODO add base library if it's not set
        # TODO (based on required factorio version)

        if all(self._dependencies.values()) or self.name == "base":
            return self._dependencies

        if self.downloaded:
            mod_file = None
            for filename in config.mods_file.parent.rglob("*.zip"):
                name = filename.stem.rsplit("_", 1)[0]
                if name == self.name:
                    mod_file = filename
                    break

            with zipfile.ZipFile(mod_file) as archive:
                info_json_path = list(
                    filter(
                        lambda zipinfo: "info.json" in zipinfo.filename,
                        archive.filelist
                    )
                )[0].filename

                dependencies_list = json.loads(
                    archive.read(info_json_path).decode('utf-8')
                ).get("dependencies") or []
        else:
            response = self._api_mod_full_info()
            release = response.json()["releases"][-1]
            dependencies_list = release["info_json"]["dependencies"]

        optional_prefixes = ["?", "(?)"]
        conflict_prefixes = ["!", "(!)"]
        parent_prefixes = ["~", "(~)"]

        for dependency_raw in dependencies_list:
            category, dependency = self._parse_dependency(
                dependency_raw,
                {
                    "optional": optional_prefixes,
                    "conflict": conflict_prefixes,
                    "parent": parent_prefixes,
                    "require": None
                }
            )
            self._dependencies[category] = (
                self._dependencies.get(category) or []
            ) + [Mod(dependency)]

        return self._dependencies

    def _api_mod_full_info(self):
        mods_url = "https://mods.factorio.com/api/mods"
        mod_url = f"{mods_url}/{self.name}"
        mod_full_url = f"{mod_url}/full"

        return requests.get(mod_full_url)

    def download(self, version: str = None, release_json=None):
        if self.downloaded:
            raise exceptions.ModAlreadyExistsError(self.name)

        if not release_json:
            response = self._api_mod_full_info()
            if response.status_code != 200:
                logger.warning(
                    f"""Could not download. Server is not responding. \
Status code: {response.status_code}""")
                return

            response_json = response.json()
            releases = response_json["releases"]
            if version is None:
                requested_release = releases[-1]
            else:
                for release in releases:
                    if version == release["version"]:
                        requested_release = release
                        break
                else:
                    raise exceptions.VersionNotFoundError(self.name, version)
        else:
            requested_release = release_json
        download_url = requested_release["download_url"]
        file_name = requested_release["file_name"]

        with config.game_folder.joinpath("player-data.json").open() as f:
            player_data = json.load(f)
            # TODO write checks if user does not have full account
            # TODO or not logged in
            username = player_data["service-username"]
            token = player_data["service-token"]

        url = f"https://mods.factorio.com/{download_url}?"
        url_params = {
            "username": username,
            "token": token
        }
        url = url + urlparse.urlencode(url_params)
        with config.mods_file.parent.joinpath(file_name).open("wb") as f:
            f.write(requests.get(url).content)

        self._downloaded = True
        self._version = requested_release["version"]

        logger.info(f"'{self.name}_{self.version}' successfully installed")

        for mod in self.dependencies["parent"] + self.dependencies["require"]:
            if not mod.downloaded:
                mod.download()

    def update(self) -> None | dict:
        """
        Check for the new version of the mod

        Parameters
        ----------

        Returns
        -------
        None | dict
        """
        if not self.downloaded:
            raise exceptions.ModNotFoundError(self.name)

        if self._has_new_version:
            return self._has_new_version

        response = self._api_mod_full_info()
        if response.status_code != 200:
            logger.warning(
                f"""Could not download. Server is not responding. \
Status code: {response.status_code}""")
            return

        releases = response.json()["releases"]
        if self.version == releases[-1]["version"]:
            self._has_new_version = None

        self._has_new_version = releases[-1]

        return self._has_new_version

    def upgrade(self):
        """
        Updates the version of the mod and downloads it

        Parameters
        ----------

        Returns
        -------
        None
        """
        if not (new_release := self.update()):
            logger.warning(
                f"The latest version of '{self.name}' already installed")
            return

        old_version = self.version
        self.remove()
        self.download(release_json=new_release)
        logger.info(
            f"""'{self.name}' successfully upgraded \
('{old_version}' -> '{self.version}')"""
        )

    def remove(self):
        """
        Removes the mod with his dependencies

        Parameters
        ----------

        Returns
        -------
        None
        """
        if self.name == "base":
            raise exceptions.BaseModRemoveError()

        if not self.downloaded:
            logger.warning(f"'{self.name}' wasn't downloaded. Ignoring")
            return

        for mod_file in config.mods_file.parent.rglob("*.zip"):
            if mod_file.stem.rsplit("_", 1)[0] == self.name:
                mod_file.unlink()
                logger.info(f"'{self.name}' mod was removed")
                break

        self._downloaded = False

        for mod in Mod.downloaded_mods():
            # delete all mods that required self.name
            # or those that self.name hard required

            # clear all parents and hard children
            if (
                self.name in map(
                    lambda m: m.name,
                    mod.dependencies["require"]
                )
                or self.name in map(
                    lambda m: m.name,
                    mod.dependencies["parent"]
                )
            ) and mod.downloaded:
                mod.remove()

        # clear all orphaned
        for dependency in self.dependencies["require"]:
            for downloaded_mod in Mod.downloaded_mods():
                if dependency.name in map(
                    lambda m: m.name,
                    (
                        downloaded_mod.dependencies["require"]
                        + downloaded_mod.dependencies["optional"]
                    )
                ):
                    break
            else:
                if dependency.downloaded:
                    dependency.remove()

    @classmethod
    def search_mods(
            cls,
            query,
            version="any",
            search_order="downloaded"
    ) -> Generator[dict] | None:
        """
        Search mod portal (mods.factorio.com) for the mod

        Parameters
        ----------
            query : str
                name of the mod to search
            version : str, optional
                version of the mod to search
            search_order : str, optional
                result sorting
                known variants: [downloaded, updated]

        Returns
        -------
        Generator[dict] | None

        """

        # TODO optimize FOR cycle

        if search_order not in ["updated", "downloaded"]:
            return

        start_index = 1
        url = f"https://mods.factorio.com/{start_index}?"
        params = {
            "query": query,
            "version": version,
            "search_order": search_order
        }
        search_query = url + urlparse.urlencode(params)

        response = requests.get(search_query)
        if response.status_code != 200:
            logger.warning(
                f"""Could not download. Server is not responding. \
    Status code: {response.status_code}""")
            return

        soup = BeautifulSoup(response.text, "lxml")

        # filter <div class="mod-list"> block
        mod_list = soup.find('div', class_="mod-list")
        last_page_index = mod_list.find_previous_sibling(
        ).find_previous_sibling().find_all("a")[-2].text.strip()

        last_index = int(last_page_index) if last_page_index != "" else 1

        for page_index in range(start_index, last_index+1):
            for block in mod_list.findChildren("div", recursive=False):
                link = block.find_all("a")[1]
                name = "/".join(link["href"].split("/")[2:])
                name_extended = link.text
                description = block.find("p", class_="pre-line").text.strip()
                yield {
                    "name": name,
                    "description": description,
                    "name_extended": name_extended
                }
            url = f"https://mods.factorio.com/{page_index+1}?"
            search_query = url + urlparse.urlencode(params)
            response = requests.get(search_query)
            soup = BeautifulSoup(response.text, "lxml")
            mod_list = soup.find('div', class_="mod-list")

    @classmethod
    @property
    def downloaded_mods(cls) -> Generator:
        """
        Check for list of downloaded mods

        Parameters
        ----------

        Returns
        -------
        Generator[Mod]
        """
        yield Mod("base")
        for filename in config.mods_file.parent.rglob("*.zip"):
            mod_name = filename.stem.rsplit("_", 1)[0]
            yield Mod(mod_name)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"""<class '{__class__.__name__}' \
name: '{self.name}', \
downloaded: {self.downloaded}>"""
