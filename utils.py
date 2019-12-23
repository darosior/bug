"""
Utility functions.
"""
import base64
import importlib
import json
import os
import re
import subprocess
import stat
import sys
import urllib.request
import zipfile
from packaging import version


def plug_debug(line):
    """
    Use $PWD/debug.log as stdout for print()-debugging a plugin
    """
    with open(os.path.join(os.getcwd(), "plugin_debug.log"), "a") as f:
        f.write(line)


def create_dir(abs_path):
    """
    Creates a directory
    """
    if not os.path.isdir(abs_path):
        subprocess.call(["mkdir", "-p", abs_path])


def make_executable(abs_path):
    """
    Add the exec permission to a file
    """
    os.chmod(abs_path, os.stat(abs_path).st_mode | stat.S_IEXEC)


def write_file(file_path, b64_string):
    """
    Creates a file and writes its content from a b64 string.
    """
    with open(file_path, 'w') as f:
        content = base64.b64decode(b64_string)
        f.write(content.decode("utf-8"))


def get_main_file(possible_filenames, install_path):
    """
    Tries to detect the main file of a plugin directory
    """
    content = os.listdir(install_path)
    if len(content) == 1:
        tmp_file = os.path.join(install_path, content[0])
        if not os.path.isdir(tmp_file):
            # There is only one file, this is the main one !
            return tmp_file
        else:
            # The archive actually contained a directory, let's clean it up
            for f in os.listdir(tmp_file):
                os.rename(os.path.join(tmp_file, f),
                          os.path.join(install_path, f))
            os.rmdir(tmp_file)
            content = os.listdir(install_path)
    # Iterate through all files that are not source files of a compiled
    # language, to check if there is the main one
    for filename in [f for f in content
                     if os.path.isfile(os.path.join(install_path, f))
                     and not re.findall(r"^.*\.cpp|\.c|\.go$",
                                        os.path.join(install_path, f))]:
        # FIXME: Improve main file detection
        for possible_filename in possible_filenames:
            if possible_filename in filename:
                return os.path.join(install_path, filename)
    return None


def dl_github_repo(install_path, url):
    """
    Downloads a whole Github repo, then delete the '.git' directory.

    :param install_path: Where to clone the repo.
    :param url: Repo url.
    """
    json_string = urllib.request.urlopen(url + "?recursive=1").read()
    json_content = json.loads(json_string.decode("utf-8"))
    for element in json_content["tree"]:
        if element["path"][0] == '.':
            continue
        if element["mode"] == "040000":
            # We'll handle subdir creation below
            continue
        abs_path = os.path.join(install_path, element["path"])
        if len(element["path"].split('/')) > 1:
            subdirs = "/".join(abs_path.split('/')[:-1])
            os.makedirs(os.path.join(install_path, subdirs), exist_ok=True)
        # Yeah, that __is__ a request inside a loop (max 5000 req/hour
        # without authentication)
        # FIXME: We could hit frontend instead ? Like raw.github.com
        blob_json_string = urllib.request.urlopen(element["url"]).read()
        blob_json = json.loads(blob_json_string.decode("utf-8"))
        assert blob_json["encoding"] == "base64"
        write_file(abs_path, blob_json["content"])
        if element["mode"] == "100755":
            make_executable(abs_path)


def dl_folder_from_github(install_path, url):
    """
    Recursively fetches files from a github repo's folder.

    :param install_path: Where to store the folder.
    :param url: From where to fetch the folder (Github API url).
    """
    if not re.search(r"[api.github.com/repos/]+[/contents/]+", url):
        raise ValueError("Unsupported url")
    json_string = urllib.request.urlopen(url).read().decode("utf-8")
    json_content = json.loads(json_string)
    if not isinstance(json_content, list):
        if "submodule_git_url" in json_content:
            dl_github_repo(install_path, json_content["git_url"])
            return
        else:
            raise ValueError("Could not parse json: {}".format(json_content))
    for i in json_content:
        if "download_url" in i:
            if i["download_url"] is not None:
                dest = os.path.join(install_path, i["name"])
                urllib.request.urlretrieve(i["download_url"], dest)
            # This is a folder
            else:
                new_install_path = os.path.join(install_path, i["name"])
                create_dir(new_install_path)
                dl_folder_from_github(new_install_path,
                                      url + i["name"] if url[:-1] == '/' else
                                      url + '/' + i["name"])
        # Unlikely
        elif "submodule_git_url" in i:
            dl_github_repo(os.path.join(install_path, i["name"]),
                           json_content["submodule_git_url"])


def handle_requirements(directory):
    """
    Handles the 'pip install's if this is a Python plugin (most are).
    """
    content = os.listdir(directory)
    for filename in content:
        if "requirements" in filename:
            with open(os.path.join(directory, filename), 'r') as f:
                for line in f:
                    # Some requirements.txt have blank lines...
                    if line not in {'\n', ' '}:
                        pip_install(line)


def handle_compilation(directory):
    """
    Handles the compilation of a GO/C/C++ plugin
    """
    content = os.listdir(directory)
    # Simple case: there is a Makefile
    for name in content:
        if name == "Makefile":
            subprocess.check_output(["make"])
            return
    # Otherwise we can still try to `go build` go plugins
    for filename in [name for name in content if '.' in name]:
        if filename.split('.')[1] == "go":
            try:
                go_bin = os.path.abspath("go")
                subprocess.check_output([go_bin, "build"])
            except (FileNotFoundError, subprocess.CalledProcessError):
                raise Exception("Could not 'go build' the plugin, is golang"
                                " installed ?")


def pip_install(package):
    """
    'pip' install a Python package if not already installed (likely, globally
    installed)
    """
    package_name = package.split("==")[0]
    if ">=" in package:
        package_name = package.split(">=")[0]
    spec = importlib.util.find_spec(package_name)
    if spec is None:
        # MUST NOT fail
        subprocess.check_output([sys.executable, "-m", "pip",
                                 "install", package])
    if "==" in package:
        package_version = version.parse(package.split("==")[1])
        try:
            installed_version = version.parse(importlib.
                                              import_module(package_name)
                                              .__version__)
            if package_version > installed_version:
                # MUST NOT fail
                subprocess.check_output([sys.executable, "-m", "pip",
                                         "install", package])
        except AttributeError:
            # No __version__ ..
            pass
