import os
import sys
import json
import tempfile
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


from utils.http_client import do_sync_request
from utils.hash import hashJSON
from utils.log import logError


# Read list file and return content
def readList(option, config):
    if option == "username":
        with open(config.USERNAME_LIST_PATH, "r", encoding="UTF-8") as f:
            data = json.load(f)
        return data
    elif option == "email":
        with open(config.EMAIL_LIST_PATH, "r", encoding="UTF-8") as f:
            data = json.load(f)
        return data
    elif option == "metadata":
        with open(config.USERNAME_METADATA_LIST_PATH, "r", encoding="UTF-8") as f:
            data = json.load(f)
        return data
    else:
        return False


def validateUsernameData(data):
    required_site_fields = {
        "name",
        "uri_check",
        "e_code",
        "e_string",
        "m_string",
        "m_code",
        "cat",
    }
    if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
        raise ValueError("Username data has an invalid structure")
    if not data["sites"]:
        raise ValueError("Username data does not contain any sites")
    if any(
        not isinstance(site, dict) or not required_site_fields.issubset(site)
        for site in data["sites"]
    ):
        raise ValueError("Username data contains an invalid site definition")
    return data


def writeUsernameData(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="UTF-8", dir=target.parent, delete=False
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(data, temp_file, indent=4, ensure_ascii=False)
    temp_path.replace(target)


# Download .JSON file list from defined URL
def downloadList(config):
    response = do_sync_request("GET", config.USERNAME_LIST_URL, config)
    if response is None or response.status_code != 200:
        raise RuntimeError("Could not download the username list")
    writeUsernameData(config.USERNAME_LIST_PATH, validateUsernameData(response.json()))


# Check for changes in remote list
def checkUpdates(config):
    target = Path(config.USERNAME_LIST_PATH)
    fallback = Path(
        getattr(config, "USERNAME_FALLBACK_PATH", config.USERNAME_LIST_PATH)
    )
    if not target.is_file() and fallback.is_file() and target != fallback:
        target.parent.mkdir(parents=True, exist_ok=True)
        with fallback.open("r", encoding="UTF-8") as file:
            bundled_data = validateUsernameData(json.load(file))
        writeUsernameData(target, bundled_data)

    local_data = None
    if target.is_file():
        try:
            with target.open("r", encoding="UTF-8") as file:
                local_data = validateUsernameData(json.load(file))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if fallback.is_file() and target != fallback:
                with fallback.open("r", encoding="UTF-8") as file:
                    local_data = validateUsernameData(json.load(file))
                writeUsernameData(target, local_data)
            else:
                logError(error, "Could not read the username list", config)

    config.console.print(":counterclockwise_arrows_button: Checking for updates...")
    update_config = config
    if hasattr(config, "dataset_timeout"):
        import copy

        update_config = copy.copy(config)
        update_config.timeout = min(config.timeout, config.dataset_timeout)

    try:
        response = do_sync_request("GET", config.USERNAME_LIST_URL, update_config)
        if response is None or response.status_code != 200:
            raise RuntimeError("The username data source is unavailable")
        remote_data = validateUsernameData(response.json())
        if local_data is None:
            writeUsernameData(target, remote_data)
            config.console.print(":globe_with_meridians: Downloaded the username list")
        elif hashJSON(local_data) != hashJSON(remote_data):
            writeUsernameData(target, remote_data)
            config.console.print(":counterclockwise_arrows_button: Updated the username list")
        else:
            config.console.print("✔️  Sites List is up to date")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        if local_data is None:
            logError(error, "Could not load the username list", config)
            raise RuntimeError("Username search data is unavailable") from error
        config.console.print(":warning: Could not update the username list; using the local copy")
        logError(error, "Could not update the username list", config)
