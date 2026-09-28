"""Keep Playwright's nested driver out of PyInstaller's binary signing pass.

The build helper copies the driver directory beside the frozen application
after PyInstaller finishes. This preserves the executable and its package
layout, while avoiding macOS treating the Node driver as a collected binary.
"""
datas = []
