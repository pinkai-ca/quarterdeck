# Forum

This is a complete ship with a lead and four independent reviewers. Copy it to a
folder of its own. Edit `ship.toml` for the actual credentials, endpoint and model
names on the recording machine. The fourth reviewer uses a local endpoint through
pi; no Muse or Google allowance is assumed. No subscriptions are queried by copying
or loading the template.

From a checkout:

```sh
cp -R ships/forum /path/to/camera-forum
# Edit /path/to/camera-forum/ship.toml for this machine.
qd --ship /path/to/camera-forum upgrade COMMIT
qd --ship /path/to/camera-forum up
qd --ship /path/to/camera-forum post order --to reviewers --body-file /path/to/brief.txt
qd --ship /path/to/camera-forum status
qd --ship /path/to/camera-forum inbox operator
```

`upgrade` installs this revision and its resources into the camera ship. Its
template is then at `runtime/venv/share/quarterdeck/ships/forum`, beside the drydock;
it can be copied for another ship. `standing.project_dir()` locates this resource
root from either the checkout or the installed Python interpreter.

Posting to `reviewers` stores an ordinary request to the lead and four orders with
different ids, the same parent request id, and identical bodies. Alternatively,
post to `lead`; its card forwards that original request to the group without making
a second root. Each report closes only its reviewer's copy. The service posts one
all-filed or named member-failure notice; the lead then combines the named reports,
keeps dissent, adds no new analysis, and reports once on the root to its requester.

The inbox and launch carriage select each seat's own messages. The shared post
store remains readable: reviewer independence is card-enforced, not a filesystem
boundary, and complete detection of reads is not promised. There is no fleet,
compiler, `qd raise`, automatic model binding or external notification here.

These commands are instructions for a separately authorized live run. The
template does not assert that any named native model or endpoint is available.
