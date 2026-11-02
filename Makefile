.PHONY: all help test deps run-desktop run-window screenshots lint clean install uninstall

PYTHON ?= python3
SRC     = src/gba_launcher.py src/gba_supervisor.py src/gba_adapter_mgba.py \
          src/gba_recovery.py src/common.py src/library.py src/config.py \
          src/theme.py src/gba_terminal.py src/gba_browser.py src/gba_usb.py
TESTS   = tests

all: test

help:
	@echo "GBA OS — available targets"
	@echo "  make deps         Install Python dependencies (pygame)"
	@echo "  make test         Run the contract test suite"
	@echo "  make lint         Byte-compile + shell syntax checks"
	@echo "  make run-window   Launch the shell in a window (desktop testing)"
	@echo "  make screenshots  Regenerate the README screenshots"
	@echo "  make install      Install the appliance (requires sudo)"
	@echo "  make uninstall    Remove the GRUB entry and system files"
	@echo "  make clean        Remove caches and temporary files"

deps:
	$(PYTHON) -m pip install --user -r requirements.txt

test:
	@$(PYTHON) -c "import pygame" 2>/dev/null || \
		(echo "pygame is required: run 'make deps' or 'pip install pygame'" && exit 1)
	$(PYTHON) -m unittest discover -s $(TESTS) -v

lint:
	@$(PYTHON) -m py_compile $(SRC) && echo "python: OK"
	@bash -n scripts/install.sh && echo "install.sh: OK"
	@bash -n scripts/launch_desktop.sh && echo "launch_desktop.sh: OK"
	@sh -n src/gba_init.sh && echo "gba_init.sh: OK"

run-window run-desktop:
	./scripts/launch_desktop.sh

screenshots:
	$(PYTHON) src/gba_launcher.py --tab games --demo --screenshot ui_preview_games.png
	$(PYTHON) src/gba_launcher.py --tab apps --demo --screenshot ui_preview_apps.png
	$(PYTHON) src/gba_launcher.py --tab settings --demo --screenshot ui_preview_settings.png
	$(PYTHON) src/gba_launcher.py --tab browser --demo --screenshot ui_preview_browser.png

install:
	./scripts/install.sh

uninstall:
	./scripts/install.sh --uninstall

clean:
	rm -rf __pycache__ src/__pycache__ tests/__pycache__ /tmp/gba*
