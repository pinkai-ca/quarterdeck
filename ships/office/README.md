# The office

One office serves the fleet. It is a ship with `kind = "office"`, with its own folder, git repository, runtime, tmux server and service. It starts with one desk and no captain or crew. There is no project workspace to choose before starting.

`qd init` creates an office from this template and starts its desk. It does not raise a project ship or move seats from an existing ship. Cross-ship messages, forwarding old addresses and raising a project ship are separate changes.

The person can declare further office seats in the office's `ship.toml`, using roles such as counsel, communications, media or steward. Each seat belongs to a session declared with `human = true`. An office cannot declare crew roles, reviewer groups or a cascade. Additional seats start only when the person brings them up.

The local desk card describes the desk's place in the fleet. Office orders and cards are committed declarations; messages, sign-ins and running state stay under ignored `runtime/`.
