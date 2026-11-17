#!/usr/bin/env python3
"""
Native LibreOffice Document Formatter via PyUNO.
Uses LibreOffice's official document layout engine to update text styling
WITHOUT touching, parsing, or corrupting document XML, section properties, or margins.
"""
import os
import sys
import time
import subprocess
import uno
from com.sun.star.beans import PropertyValue

DOC_PATH = os.path.abspath("/home/arean/Documents/Documents001/Redmine/Redmine_7_0_3_Installation_Guide.docx")
PORT = 2002

def start_libreoffice():
    # Start headless LibreOffice listening on a socket
    cmd = [
        "libreoffice",
        "--headless",
        "--invisible",
        "--nocrashreport",
        "--nodefault",
        "--nologo",
        "--nofirststartwizard",
        f"--accept=socket,host=localhost,port={PORT};urp;StarOffice.ComponentContext"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    return proc

def format_appendix():
    if not os.path.exists(DOC_PATH):
        print(f"Error: {DOC_PATH} does not exist.")
        return

    proc = start_libreoffice()
    try:
        local_context = uno.getComponentContext()
        resolver = local_context.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_context
        )
        
        # Connect to running LibreOffice process
        context = resolver.resolve(f"uno:socket,host=localhost,port={PORT};urp;StarOffice.ComponentContext")
        smgr = context.ServiceManager
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", context)

        # Load document natively
        file_url = uno.systemPathToFileUrl(DOC_PATH)
        prop_hidden = PropertyValue("Hidden", 0, True, 0)
        doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (prop_hidden,))

        # Search for Appendix title descriptor
        search_desc = doc.createSearchDescriptor()
        search_desc.SearchString = "Appendix: Production Operations Quick-Reference Cheat Sheet"
        found = doc.findFirst(search_desc)

        if found:
            # Apply heading styling natively via LibreOffice DOM
            # CharWeight: Bold (150%)
            # CharHeight: 12 pt
            # CharColor: #0F2043 (RGB: 15, 32, 67 -> 0x0F2043 = 991299)
            found.CharWeight = 150.0
            found.CharHeight = 12.0
            found.CharColor = 0x0F2043
            print("Successfully found and styled Appendix heading via native LibreOffice engine.")
        else:
            print("Appendix heading text not found.")

        # Save document natively in its original format without touching margins or page layout
        doc.store()
        doc.close(True)
        print(f"Document saved natively: {DOC_PATH}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

if __name__ == "__main__":
    format_appendix()
