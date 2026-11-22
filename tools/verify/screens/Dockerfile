# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# The browser image of the screenshot stack: a pinned Playwright base (browsers and system
# libraries) plus the Python package of the same version, so the stack needs no network to run.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble@sha256:38dd67fc654b56b3cba611e4c8eb71cd33750e1cd29eea2a5dc00b484817a75f
COPY constraints.txt /constraints.txt
RUN pip install --no-cache-dir -c /constraints.txt playwright==1.65.2
