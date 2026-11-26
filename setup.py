#!/usr/bin/env python3
"""
Setup script for the CM:EX Linux Player.
"""

from pathlib import Path
from setuptools import setup, find_packages

HERE = Path(__file__).parent


def _requirements(name: str) -> list:
    lines = (HERE / name).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("#", "-r"))]


setup(
    name="signage-player",
    version="2.5.0",
    description="CM:EX Linux digital signage player",
    long_description=(HERE / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="DVSignage",
    python_requires=">=3.10",
    packages=find_packages(include=["cmx", "cmx.*", "ndi", "ndi.*"]),
    package_data={"cmx.renderer": ["static/*"]},
    py_modules=["main"],
    install_requires=_requirements("requirements.txt"),
    extras_require={"ndi": _requirements("requirements-ndi.txt")},
    entry_points={
        "console_scripts": [
            "signage-player=main:main",
        ],
    },
    classifiers=[
        "Intended Audience :: System Administrators",
        "Topic :: Multimedia :: Video :: Display",
        "Programming Language :: Python :: 3",
        "Operating System :: POSIX :: Linux",
    ],
)
