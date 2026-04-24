# 5. Workers never see the customer's message

Status: accepted. Only intake reads the raw text, with no tools. Downstream nodes see the structured summary; workers get one question and ids. Tool output (including free-text DB fields) is wrapped in labelled `<data>` blocks, tools are read-only, and the acting node uses no model. `customer_id` and "photo supplied" come from the session and the upload, never from the text. Tested with a compromised model that obeys every injection.
