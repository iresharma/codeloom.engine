#!/usr/bin/env python3
"""Quick validation that new test files can be imported."""
from __future__ import annotations

import sys

try:
    # Test imports
    import tests.test_httpx_coverage
    import tests.test_github_coverage
    import tests.test_tools_github_coverage
    
    print("✓ All test modules imported successfully")
    
    # Count test classes
    import inspect
    
    httpx_tests = [
        name for name, obj in inspect.getmembers(tests.test_httpx_coverage)
        if inspect.isclass(obj) and name.startswith("Test")
    ]
    
    github_tests = [
        name for name, obj in inspect.getmembers(tests.test_github_coverage)
        if inspect.isclass(obj) and name.startswith("Test")
    ]
    
    tools_github_tests = [
        name for name, obj in inspect.getmembers(tests.test_tools_github_coverage)
        if inspect.isclass(obj) and name.startswith("Test")
    ]
    
    print(f"✓ test_httpx_coverage.py: {len(httpx_tests)} test classes")
    print(f"✓ test_github_coverage.py: {len(github_tests)} test classes")
    print(f"✓ test_tools_github_coverage.py: {len(tools_github_tests)} test classes")
    
    total_classes = len(httpx_tests) + len(github_tests) + len(tools_github_tests)
    print(f"\n✓ Total: {total_classes} test classes created")
    
    sys.exit(0)
except Exception as e:
    print(f"✗ Error: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
