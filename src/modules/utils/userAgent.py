import random
import os


def getRandomUserAgent(config):
    path = os.path.join(
        os.path.join(os.path.dirname(__file__)),
        "..",
        "..",
        "..",
        "data",
        "useragents.txt",
    )
    with open(path, encoding="utf-8") as file:
        userAgents = file.read().splitlines()
    userAgent = random.choice(userAgents)
    if config.verbose:
        config.console.print(f':id: Selected random User-Agent "{userAgent}"')
    return userAgent
