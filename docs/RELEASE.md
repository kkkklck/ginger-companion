# Release contents

This repository publishes the Ginger Companion local application and its interface. The four core Python modules, web assets, and launcher come from the author's local project.

Personal settings, API keys, field databases, paper PDFs, and extracted evidence packages remain on the author's machine. A new installation starts with empty local data and can use the built-in demonstration field. Paper retrieval becomes available after compatible materials are connected locally.

The separate extraction script `ginger_qwen_hybrid.py` and its configuration are not included. Historical local notes may refer to that workflow; running it requires the separate script and its environment.

The developer evidence-review interface retains its page structure with an empty offline snapshot. When opened through the application, it reads local evidence.

The core application uses the Python standard library. Some original-page-image provenance checks optionally use `pymupdf`, together with the original local PDF.

An open-source license has not yet been specified. Public code visibility does not itself grant unrestricted use or redistribution rights.
