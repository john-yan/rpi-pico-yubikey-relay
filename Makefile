# Makefile — YubiKey USB relay for Raspberry Pi Pico (CircuitPython)
#
# Overridable paths (mount points vary by desktop):
#   make deploy CIRCUITPY=/path/to/CIRCUITPY

CIRCUITPY ?= /run/media/$(USER)/CIRCUITPY
BOOTSEL   ?= /run/media/$(USER)/RPI-RP2

# CircuitPython image to download (see https://circuitpython.org/board/raspberry_pi_pico/)
CP_VERSION ?= 10.3.1
CP_URL = https://downloads.circuitpython.org/bin/raspberry_pi_pico/en_US/adafruit-circuitpython-raspberry_pi_pico-en_US-$(CP_VERSION).uf2

RULES_FILE = 70-pico-yubikey-relay.rules
RULES_DEST = /etc/udev/rules.d/$(RULES_FILE)

.PHONY: help download flash deploy deploy-debug reset test e2e check debug log install-rules remove-rules clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

download: ## Download the CircuitPython image (VERSION=10.3.1)
	@curl -L -o circuitpython-pico-$(CP_VERSION).uf2 "$(CP_URL)"
	@echo "Downloaded circuitpython-pico-$(CP_VERSION).uf2 (git-ignored)."
	@echo "Flash it with: make flash UF2=circuitpython-pico-$(CP_VERSION).uf2"

flash: ## Flash firmware (Pico in BOOTSEL mode): make flash UF2=<file.uf2>
	@test -n "$(UF2)" || { echo "usage: make flash UF2=<file.uf2> (or run: make download)"; exit 2; }
	@test -d "$(BOOTSEL)" || { \
		echo "RPI-RP2 drive not found at $(BOOTSEL)"; \
		echo "Hold BOOTSEL while plugging the Pico in, then retry."; \
		exit 1; }
	@cp "$(UF2)" "$(BOOTSEL)/"
	@sync
	@echo "Flashed $(UF2) - the Pico reboots into the new firmware."

deploy: ## Copy firmware sources (boot.py, code.py, lib/) to CIRCUITPY
	@test -d "$(CIRCUITPY)" || { \
		echo "CIRCUITPY drive not found at $(CIRCUITPY)"; \
		echo "Is the Pico plugged in and running CircuitPython?"; \
		exit 1; }
	@cp src/boot.py src/code.py "$(CIRCUITPY)/"
	@mkdir -p "$(CIRCUITPY)/lib"
	@cp src/lib/*.py "$(CIRCUITPY)/lib/"
	@sync
	@echo "Deployed. code.py auto-reloads; boot.py changes also need: make reset"

deploy-debug: ## Deploy the tracing build (code_debug.py) as code.py
	@test -d "$(CIRCUITPY)" || { echo "CIRCUITPY not mounted at $(CIRCUITPY)"; exit 1; }
	@cp src/code_debug.py "$(CIRCUITPY)/code.py"
	@sync
	@echo "WARNING: tracing build deployed - it logs CTAPHID packet headers"
	@echo "to the serial console. Redeploy production code with: make deploy"

reset: ## Hard-reset the board (applies boot.py changes; GP5 re-attaches YubiKey)
	python3 utils/send_repl.py "exec('import microcontroller\nmicrocontroller.reset()')" 3 || true
	@echo "Board resetting (LED blinks until the YubiKey attaches)..."

test: ## Run the host-side CTAPHID verification (no deploy)
	python3 test/test_ctaphid.py

e2e: ## Full pipeline: deploy production code, soft-reload, verify, capture log
	python3 test/e2e_test.py

check: ## Verify code_debug.py is in sync with code.py
	python3 src/make_debug.py --check

debug: ## Regenerate code_debug.py from code.py
	python3 src/make_debug.py

log: ## Passively listen to the relay console (SECS=15)
	python3 utils/listen_repl.py $(SECS)

install-rules: ## Install the udev rule so the user can access the relay's hidraw node
	@if [ -f "$(RULES_DEST)" ] && cmp -s "$(RULES_FILE)" "$(RULES_DEST)"; then \
		echo "udev rule already installed and up to date."; \
	else \
		sudo cp "$(RULES_FILE)" "$(RULES_DEST)"; \
		sudo udevadm control --reload-rules; \
		sudo udevadm trigger; \
		echo "Installed $(RULES_DEST) (browsers can now reach the relay)."; \
	fi

remove-rules: ## Remove the udev rule (revokes user access to the relay's hidraw node)
	@if [ -f "$(RULES_DEST)" ]; then \
		sudo rm "$(RULES_DEST)"; \
		sudo udevadm control --reload-rules; \
		sudo udevadm trigger; \
		echo "Removed $(RULES_DEST)."; \
	else \
		echo "udev rule is not installed."; \
	fi

clean: ## Remove Python cache dirs
	@rm -r __pycache__ test/__pycache__ 2>/dev/null || true
