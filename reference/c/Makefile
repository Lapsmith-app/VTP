CFLAGS ?= -std=c99 -O2 -Wall -Wextra -Werror -pedantic

# Sanitised builds are not an optional extra here. Every defect the producer
# suite was written for -- a count with no array behind it, an output buffer
# modified by a call that then reported failure -- is invisible to an
# optimising build and loud under ASan/UBSan.
SANFLAGS ?= -std=c99 -O1 -g -Wall -Wextra -Werror -pedantic \
            -fsanitize=address,undefined -fno-omit-frame-pointer

CODEC := vtp1.c vtp1_encode.c
HDRS  := vtp1.h vtp1_encode.h vtp1_generated.h

all: vtp1_cli vtp1_producer

vtp1_cli: vtp1_cli.c $(CODEC) $(HDRS)
	$(CC) $(CFLAGS) -o $@ vtp1_cli.c $(CODEC)

# The producer-conformance adapter (conformance/produce.py). Separate from the
# decoder adapter because the two speak different contracts and an
# implementation may honestly ship only one of them.
vtp1_producer: vtp1_producer.c vtp1_encode.c vtp1_encode.h vtp1.h vtp1_generated.h
	$(CC) $(CFLAGS) -o $@ vtp1_producer.c vtp1_encode.c

# The encoder's C-API contract, which no wire vector can reach: an array that
# is not there, and "nothing is written on failure". Built with sanitisers
# because a null dereference that happens to survive is not a pass.
encode_selftest: encode_selftest.c vtp1_encode.c $(HDRS)
	$(CC) $(SANFLAGS) -o $@ encode_selftest.c vtp1_encode.c

.PHONY: all test san clean
test: vtp1_cli vtp1_producer
	cd ../.. && python3 conformance/run.py --impl "reference/c/vtp1_cli"
	cd ../.. && python3 conformance/produce.py --impl "reference/c/vtp1_producer"

# The whole suite over a sanitised build, plus the C-API contract test.
san: encode_selftest
	./encode_selftest
	$(CC) $(SANFLAGS) -o vtp1_cli_san vtp1_cli.c $(CODEC)
	$(CC) $(SANFLAGS) -o vtp1_producer_san vtp1_producer.c vtp1_encode.c
	cd ../.. && python3 conformance/run.py --impl "reference/c/vtp1_cli_san"
	cd ../.. && python3 conformance/produce.py --impl "reference/c/vtp1_producer_san"

clean:
	rm -f vtp1_cli vtp1_producer encode_selftest vtp1_cli_san vtp1_producer_san
