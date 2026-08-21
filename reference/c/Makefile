CFLAGS ?= -std=c99 -O2 -Wall -Wextra -Werror -pedantic

vtp1_cli: vtp1_cli.c vtp1.c vtp1_encode.c vtp1.h vtp1_encode.h vtp1_generated.h
	$(CC) $(CFLAGS) -o $@ vtp1_cli.c vtp1.c vtp1_encode.c

.PHONY: test clean
test: vtp1_cli
	cd ../.. && python3 conformance/run.py --impl "reference/c/vtp1_cli"

clean:
	rm -f vtp1_cli
