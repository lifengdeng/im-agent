"""Launch unattended CLI children without allocating a Windows console.

Redirecting pipes or using pythonw for the parent does not suppress a child's
console. Keep interactive commands (such as a manual login) outside this policy.
"""
import os
import subprocess


def background_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}
