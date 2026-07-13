# react-text-insertion

**Trigger:** Normal typing into a React-controlled editor corrupts component
state, drops characters, or fails to stick.

**Rule:** Focus the target and insert via native text insertion, not raw
keystroke spam.

**Steps:**
1. Focus the editable element.
2. Insert with `document.execCommand('insertText', false, text)`.
3. Verify the visible value matches the intended draft before submitting.

**Palace:** `palace_search("document.execCommand insertText React", room="knowledge")`
