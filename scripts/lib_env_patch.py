#!/usr/bin/env python3
"""Emit the strategic-merge patch that sets FLINK_PROPERTIES to $UPDATED.

Its own file because building this JSON inline from bash needs quoting that
survives a multi-line value, which is exactly what broke profile-operators.sh.
"""
import json, os
print(json.dumps({"spec": {"template": {"spec": {"containers": [
    {"name": "taskmanager",
     "env": [{"name": "FLINK_PROPERTIES", "value": os.environ["UPDATED"]}]}]}}}}))
