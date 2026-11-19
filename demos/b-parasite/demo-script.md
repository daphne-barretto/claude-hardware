# Demo script

The end-to-end walk through the app that the session API tests follow. Board-specific details come from `demos/b-parasite/CHANGES.md`; this script names no parts on purpose.

Claude Hardware is a general tool: anyone can ask for any part. The board revision below is one example the general pipeline handles, started from a demo in Recents; nothing in the app is written for it. Step 1 shows both. A weak design from today's model is acceptable; a request the app can't attempt is a failure.

1. **Start.** Open the app and press New: an empty chat and canvas. Ask for a part in plain words (for example "a box 40 x 30 x 20 mm with 2 mm walls"); Claude shows it is working, builds the part, shows it on the canvas and reports the applicable checks. Then open the "b-parasite case on v1.2" demo from Recents (tagged Demo): it opens as a session of your own, with its chat, and its canvas at once shows the existing enclosure for the previous board revision, and every check passes. Everything you do from here changes only your copy; **Reset demo** in its row menu starts it over.
2. **The new revision arrives.** In that session, press **Load next revision** to attach the next board revision. Claude starts the work without being asked: it lists what changed in the board, which checks now fail, and where on the model.
3. **Claude proposes the fix.** The proposed enclosure changes appear as tracked changes on the model (ghost overlay), each with the check results that prove it. Every difference in `demos/b-parasite/CHANGES.md` is addressed.
4. **Review.** Accept some changes, reject one, and ask Claude about a change by selecting a face and typing a question. The selection is sent as context.
5. **Direct edit.** Change a dimension by hand. It becomes a locked parameter, and Claude reworks the rest of the design around it.
6. **Conflict.** Lock a value that cannot satisfy the standards. Claude shows the can't-satisfy state: which standards conflict, by how much, and the options.
7. **Comment.** Pin a comment to a face. It stays attached after the model rebuilds.
8. **Compare.** Compare the two revisions side by side and open the history.
9. **Export.** Export STEP and STL plus the build note, and check that the files open.

Pass criteria: every step works without a page reload, the part asked for in step 1 is built and shown, and the board example's final design passes every applicable check in `standards.yaml`.
