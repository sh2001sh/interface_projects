# J Protocol XML X-Style Export

This export contains the current intermediate J-protocol XML extraction results produced from the PDF analysis helper project.

Contents:

- `generated_xml_xstyle/`
  - `J.xml`
  - `J*.xml`
  - `summary.json`
- `generator_snapshot/xstyle_xml_generation.py`

Source context:

- PDF source:
  - `/nfs/615/interface_projects/04_semantic_chunk/data/pageindex_uploads/shared/upload_1777373113_8820807e/upload-3670515503005183681.pdf`
- Generator workspace:
  - `/nfs/615/pdf_protocol_lab`

Current status:

- These files are an X-style structural draft for J messages.
- They are useful for XML structure review and downstream comparison against X-family XML.
- They are not yet functionally equivalent to formal X protocol XML, because some messages still need further section alignment and rule-layer completion.

Representative status at export time:

- `J16.1.xml`: structure is relatively stable and close to the PDF field layout.
- `J3.4.xml`: substantially cleaned and structurally usable.
- `J14.2.xml`: improved, but still needs section/variant alignment.
- `J12.0.xml`: noise is reduced, but some continuation-word grouping still needs refinement.
