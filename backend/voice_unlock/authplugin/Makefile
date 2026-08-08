# JARVIS Authorization Plugin -- build only.
#
# There is deliberately NO `install` target. Installation rewrites the
# authorization database that stands between the operator and their own locked
# machine, and it has an ordering constraint the build system cannot express:
# each component's code signing requirement must be COMPUTED from the signed
# binary before the next component can be configured to trust it. That lives in
# install/install.sh, where it can be read, and undone by install/uninstall.sh.
#
# `make` here produces artifacts. Nothing it does touches system state.

SHELL := /bin/bash

# --- Toolchain --------------------------------------------------------------
CC       ?= clang
SDK      := $(shell xcrun --sdk macosx --show-sdk-path 2>/dev/null)
ARCHS    ?= -arch $(shell uname -m)

# -Werror on purpose. This code runs inside SecurityAgent; a warning here is a
# defect in the authentication path, not style feedback.
CFLAGS   := -fobjc-arc -fmodules -Wall -Wextra -Werror -O2 $(ARCHS)
ifneq ($(SDK),)
CFLAGS   += -isysroot $(SDK)
endif

SRC         := src
BUILD       := build
DIST        := dist
INSTALL_DIR := install

# --- Components -------------------------------------------------------------
# JARVISGrantProtocol.m is linked by all three: it defines the symbols the
# shared header declares. Defining them in any one component's translation unit
# is what previously left the others with undefined symbols.
SHARED_SRC    := $(SRC)/JARVISGrantProtocol.m

PLUGIN_SRC    := $(SRC)/JARVISUnlockMechanism.m $(SRC)/JARVISUnlockConfig.m $(SHARED_SRC)
BROKER_SRC    := $(SRC)/JARVISUnlockBroker.m $(SHARED_SRC)
HELPER_SRC    := $(SRC)/jarvis_unlock_grant.m $(SHARED_SRC)

PLUGIN_NAME   := JARVISUnlock
BUNDLE        := $(DIST)/$(PLUGIN_NAME).bundle
BUNDLE_BIN    := $(BUNDLE)/Contents/MacOS/$(PLUGIN_NAME)
BROKER_BIN    := $(DIST)/jarvis-unlock-broker
HELPER_BIN    := $(DIST)/jarvis-unlock-grant

FRAMEWORKS    := -framework Foundation -framework Security -framework CoreGraphics -framework Carbon

.PHONY: all clean verify help
.DEFAULT_GOAL := all

all: $(BUNDLE) $(BROKER_BIN) $(HELPER_BIN) ## Build every artifact
	@echo "built into $(DIST)/ -- nothing installed; see install/install.sh"

# --- Plugin bundle ----------------------------------------------------------
# A loadable bundle, not a dylib: SecurityAgent loads bundles from
# SecurityAgentPlugins and resolves AuthorizationPluginCreate from the bundle
# executable.
$(BUNDLE): $(PLUGIN_SRC) $(SRC)/Info.plist
	@mkdir -p $(BUNDLE)/Contents/MacOS
	$(CC) $(CFLAGS) -bundle $(FRAMEWORKS) -I $(SRC) -o $(BUNDLE_BIN) $(PLUGIN_SRC)
	@cp $(SRC)/Info.plist $(BUNDLE)/Contents/Info.plist
	@echo "BNDL????" > $(BUNDLE)/Contents/PkgInfo
	@# Fail loudly if the entry point is absent. A bundle that loads but exports
	@# no AuthorizationPluginCreate leaves the authorization chain naming a
	@# mechanism that cannot be created -- a broken lock screen, discovered at
	@# the lock screen.
	@nm -gU $(BUNDLE_BIN) | grep -q '_AuthorizationPluginCreate' \
		|| { echo "FATAL: $(BUNDLE_BIN) does not export _AuthorizationPluginCreate"; exit 1; }
	@echo "  ✓ $(BUNDLE) (entry point verified)"

$(BROKER_BIN): $(BROKER_SRC)
	@mkdir -p $(DIST)
	$(CC) $(CFLAGS) $(FRAMEWORKS) -I $(SRC) -o $@ $(BROKER_SRC)
	@echo "  ✓ $@"

$(HELPER_BIN): $(HELPER_SRC)
	@mkdir -p $(DIST)
	$(CC) $(CFLAGS) $(FRAMEWORKS) -I $(SRC) -o $@ $(HELPER_SRC)
	@echo "  ✓ $@"

# --- Verification -----------------------------------------------------------
verify: all ## Assert structural invariants on the built artifacts
	@echo "verifying built artifacts..."
	@nm -gU $(BUNDLE_BIN) | grep -q '_AuthorizationPluginCreate' \
		&& echo "  ✓ plugin exports AuthorizationPluginCreate"
	@# The refusal paths are the security-critical behaviour, so they are
	@# exercised against the real binary rather than trusted.
	@env -i $(abspath $(BROKER_BIN)) >/dev/null 2>&1; \
		test $$? -ne 0 && echo "  ✓ broker refuses to start with no config" \
		|| { echo "  FATAL: broker started without configuration"; exit 1; }
	@env -i $(abspath $(HELPER_BIN)) >/dev/null 2>&1; \
		test $$? -eq 64 && echo "  ✓ helper rejects missing arguments (EX_USAGE)" \
		|| { echo "  FATAL: helper accepted no arguments"; exit 1; }
	@env -i $(abspath $(HELPER_BIN)) probe >/dev/null 2>&1; \
		test $$? -eq 78 && echo "  ✓ helper refuses without config (EX_CONFIG)" \
		|| { echo "  FATAL: helper ran without configuration"; exit 1; }
	@# The authorization rule layer. Not an artifact check, but it belongs to the
	@# same guarantee: install.sh runs `make verify` as its first step, so the code
	@# that decides whether a right may be converted into a mechanism chain is
	@# exercised before any install can reach it. That decision was previously a
	@# template literal with no test of any kind, and it black-screened the machine.
	@$(INSTALL_DIR)/test_rule_shape.sh
	@echo "all structural invariants hold"

clean: ## Remove build artifacts
	@rm -rf $(BUILD) $(DIST)
	@echo "cleaned"

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'
