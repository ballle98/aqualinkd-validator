CC := i686-w64-mingw32-gcc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror
BUILD_DIR ?= build
TARGET := $(BUILD_DIR)/pwrcntr-control.exe
CHECKSUM := $(TARGET).sha256

.PHONY: all clean package

all: $(TARGET)

package: $(TARGET)
	cd $(BUILD_DIR) && sha256sum pwrcntr-control.exe > pwrcntr-control.exe.sha256

$(TARGET): pwrcntr-control.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -municode -static-libgcc -o $@ $<

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -f $(TARGET) $(CHECKSUM)
