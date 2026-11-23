# Build the KDE KIdleTime helper. Requires a C++ compiler, pkg-config,
# Qt6 (or Qt5) and KIdleTime (KF6 or KF5).
#
# Arch / CachyOS / EndeavourOS:
#   sudo pacman -S --needed gcc pkgconf qt6-base kidletime
# Debian / Ubuntu:
#   sudo apt install --no-install-recommends g++ pkg-config qt6-base-dev libkf6idletime-dev
# Fedora:
#   sudo dnf install gcc-c++ pkgconf-pkg-config qt6-qtbase-devel kf6-kidletime-devel

CXX      ?= g++
CXXFLAGS ?= -O2 -Wall -Wextra -fPIC
PKGS     :=
TARGET   := idle_helper

ifeq ($(shell pkg-config --exists Qt6Gui Qt6Core && echo yes),yes)
PKGS += Qt6Gui Qt6Core
else ifeq ($(shell pkg-config --exists Qt5Gui Qt5Core && echo yes),yes)
PKGS += Qt5Gui Qt5Core
else
$(warning pkg-config cannot find Qt6Gui/Qt6Core or Qt5Gui/Qt5Core)
endif

ifeq ($(shell pkg-config --exists KF6IdleTime && echo yes),yes)
PKGS += KF6IdleTime
else ifeq ($(shell pkg-config --exists KF5IdleTime && echo yes),yes)
PKGS += KF5IdleTime
else
$(warning pkg-config cannot find KF6IdleTime or KF5IdleTime)
endif

PKG_CFLAGS := $(shell pkg-config --cflags $(PKGS) 2>/dev/null)
PKG_LIBS   := $(shell pkg-config --libs $(PKGS) 2>/dev/null)

.PHONY: all clean

all: $(TARGET)

$(TARGET): idle_helper.cpp
	@if [ -z "$(PKGS)" ]; then echo "Missing Qt / KIdleTime development packages. See the comments at the top of this Makefile." >&2; exit 1; fi
	$(CXX) $(CXXFLAGS) -o $@ $< $(PKG_CFLAGS) $(PKG_LIBS)

clean:
	rm -f $(TARGET)
