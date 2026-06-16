"""
Build script to package copilot_bridge as a pure-Python wheel.

Usage:
    python -m build   (recommended)
    python setup.py bdist_wheel

Output:
    dist/copilot_bridge-5.1.1-py3-none-any.whl
"""

from pathlib import Path
from setuptools import setup

# README lives alongside this file (copied here during CI; falls back to parent for local builds)
_here = Path(__file__).parent
_readme = _here / "README.md"
if not _readme.exists():
    _readme = _here.parent / "README.md"
README = _readme.read_text(encoding="utf-8") if _readme.exists() else ""

setup(
    name="copilot_bridge",
    version="5.1.5",
    description="HTTP bridge that exposes VS Code + GitHub Copilot Chat to external Python scripts and agents",
    long_description=README,
    long_description_content_type="text/markdown",
    author="Rakshith BN",
    url="https://github.com/rakshithbn-proj/copilot-bridge",
    project_urls={
        "Source": "https://github.com/rakshithbn-proj/copilot-bridge",
        "Bug Tracker": "https://github.com/rakshithbn-proj/copilot-bridge/issues",
    },
    python_requires=">=3.10",
    install_requires=["requests>=2.0"],
    py_modules=["copilot_bridge"],
    package_data={"": ["*.pyi"]},
    zip_safe=False,
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    keywords=["copilot", "vscode", "bridge", "ai", "agent", "llm", "automation"],
)
