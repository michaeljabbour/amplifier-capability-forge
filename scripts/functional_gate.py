#!/usr/bin/env python3
"""Meaningful local functional checks; no provider calls or production activation."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

class Gate(unittest.TestCase):
    def test_native_client_help_is_independent_of_daemon(self):
        r=subprocess.run([sys.executable,str(ROOT/'tools/forge.py'),'--help'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr);self.assertIn('doctor',r.stdout)

if __name__=='__main__':unittest.main(verbosity=2)
