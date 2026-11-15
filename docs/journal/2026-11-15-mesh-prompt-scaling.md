# 2026-11-15 Mesh prompt: boundary-layer estimate as a scaling

## 2026-11-15 decision: the Mesh prompt gives the scaling of a laminar wall layer, not the plate formula

`prompts/mesh.md` had said that a laminar layer on a plate of length L is about 5 L / sqrt(Re_L) thick. `cfd-reviewer` noted, during the review of the generality guard (PR 87), that this is the closed form of one quantity of the planned flat-plate generality task, which the lint cannot see. On the owner's approval the sentence now gives only the scaling (thin compared with L, growing like L / sqrt(Re_L) up to a factor of order one to ten) and says which way the margin goes (refine further out than the scaling, size the first cell on the fine side and for the thinnest region). The Mesher needs the order of magnitude only; the change removes a reference value from the prompt before the M3 freeze. The prompt version (a content hash) changes; no test pins it.
