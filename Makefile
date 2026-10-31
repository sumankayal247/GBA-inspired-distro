.PHONY: all test install run-desktop clean

all: test

test:
	python3 -m unittest discover tests

install:
	sudo ./scripts/install.sh

run-desktop:
	./scripts/launch_desktop.sh

screenshots:
	python3 src/gba_launcher.py --tab 0 --screenshot ui_preview_games.png
	python3 src/gba_launcher.py --tab 1 --screenshot ui_preview_apps.png
	python3 src/gba_launcher.py --tab 2 --screenshot ui_preview_settings.png

clean:
	rm -rf __pycache__ src/__pycache__ tests/__pycache__ /tmp/gba*
