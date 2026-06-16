"""
Build script to package copilot_bridge as a pure-Python wheel.

Usage:
    python setup.py bdist_wheel
    # or: pip install build && python -m build

Output:
    dist/copilot_bridge-5.1.0-py3-none-any.whl
"""

from setuptools import setup

setup(
    name="copilot_bridge",
    version="5.1.0",
    description="Copilot Bridge — Python client for the VS Code Copilot Bridge extension",
    long_description=(
        "CopilotBridge and CopilotAgent let you drive VS Code Copilot Chat "
        "from Python scripts. Requires the Copilot Bridge VS Code extension running."
    ),
    author="Rakshith BN",
    url="https://github.com/rakshithbn-proj/copilot-bridge",
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
    ],
)
