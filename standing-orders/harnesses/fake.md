# Program: the fake harness

This harness is native to provider Synthetic; its paying pool is declared separately in the ship file.

You are running in `qd-fakeharness`, the stand-in the framework drills with. It takes a message, runs the lines beginning `$ ` as commands, and replies. It has no model behind it: a drill reads what it did, in files and in the post store, never what it said.
