import os
import sys
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ""))

from utils.log import logError
from ..export.file_operations import safeIdentifier


# Dump HTML data to a .html file
def dumpContent(path, site, response, config):

    siteName = safeIdentifier(site["name"])
    content = response["content"]
    extension = "txt"

    content_type = response["headers"].get("Content-Type", "")
    if content_type:
        if "application/json" in content_type:
            extension = "json"
            content = response["json"]
        elif "text/html" in content_type:
            extension = "html"
            content = response["content"]

    fileName = f"{siteName}.{extension}"
    path = os.path.join(path, fileName)

    try:
        with open(path, "w", encoding="utf-8") as file:
            if response["json"]:
                json.dump(content, file)
            else:
                file.write(content)
        return True
    except Exception as e:
        logError(e, f"Coudn't DUMP data to HTML file!", config)
        return False
