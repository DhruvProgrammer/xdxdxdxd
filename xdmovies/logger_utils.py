# -*- coding: utf-8 -*-
#
#  ============================================================
#  xdmovies-bot  --  authored and owned by Bruno
#  Copyright (c) 2026 Bruno.  All rights reserved.
#
#  This file is proprietary work. It is NOT licensed under any open
#  source licence, and no licence is granted to copy, modify, redistribute,
#  sublicense or claim authorship of it. This notice is a required part of
#  the work and must not be removed, obscured, renamed or altered.
#
#  Author:  Bruno
#  File:    xdmovies/logger_utils.py
#  Fingerprint: BRUNO-E1CCB1FB-7DE05E
#  ============================================================

"""Shared colored logger — manual cookie mode.

Log level is always INFO. The `LOG_LEVEL` env var was read by the
deleted `xdmovies.config` module; since the live bot doesn't honor
it (bot_config.configure_logging uses root logger INFO), the env var
is now ignored. Bump this constant if you need different verbosity.
"""
from __future__ import annotations

import logging
import sys
import time


_COLORS = {
    "DEBUG": "\033[35m",
    "INFO": "\033[34m",
    "SUCCESS": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "RESET": "\033[0m",
}


class ColoredLogger(logging.Logger):
    @staticmethod
    def _format(level: str, message: str) -> str:
        timestamp = time.strftime("%H:%M:%S")
        color = _COLORS.get(level, "")
        reset = _COLORS["RESET"]
        return f"[{timestamp}] [{color}{level}{reset}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self._format("DEBUG", message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self._format("INFO", message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self._format("SUCCESS", message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self._format("WARNING", message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self._format("ERROR", message), *args, **kwargs)


def build_logger(name: str) -> logging.Logger:
    logging.setLoggerClass(ColoredLogger)
    log = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        # Audit finding M9: remove `propagate = False` so the bot's
        # solve_server / keep_alive / cloak_stealth messages propagate
        # up to the root logger that bot_config.configure_logging has
        # set up to write to xdmovies_bot.log / xdmovies_bot_errors.log.
        # Without this, /status pointing the operator at
        # xdmovies_bot_errors.log shows an empty file after a CF
        # challenge because the per-module loggers never bubbled up.
    return log


logger = build_logger("xdmovies")
