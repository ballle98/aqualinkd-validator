CC := i686-w64-mingw32-gcc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror
BUILD_DIR ?= build
TARGET := $(BUILD_DIR)/pwrcntr-control.exe

.PHONY: all clean

all: $(TARGET)

$(TARGET): pwrcntr-control.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -municode -static-libgcc -o $@ $<

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -f $(TARGET)
