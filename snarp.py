#!/usr/bin/env python
#
# snarp - Simple Noise Activated Recording in Python
#
# Copyright (C) 2013  Christopher Casebeer <christopher@chc.name>
# Copyright (C) 2011  Merlijn Wajer <merlijn@wizzup.org>
# Copyright (C) 2011  Antonio Ospite <ospite@studenti.unina.it>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
logging.basicConfig(level=logging.DEBUG)

import wave
import time
try:
    import cStringIO as StringIO
except ImportError:
    import StringIO

import struct
import sys
import argparse
import contextlib
import itertools

SILENCE_MIN = 120
SILENCE_MAX = 135

# Global vars for Wave format metadata
# Usually these are fixed by the format (little, 8 bit -> unsigned, > 8 bit signed), 
# but change these values to override
INPUT_ENDIANNESS = 'little' # Wave format default
INPUT_SIGNEDNESS = None # None means permit Wave format rules to prevail

CHUNK_SECONDS = 1.0 # seconds per silence analysis period


# Context managers for global Wave format overrides
@contextlib.contextmanager
def input_endianness(val):
    assert(val in ('little', 'big'))
    global INPUT_ENDIANNESS
    previous = INPUT_ENDIANNESS
    INPUT_ENDIANNESS = val
    yield
    INPUT_ENDIANNESS = previous

@contextlib.contextmanager
def input_signedness(val):
    assert(val in (None, 'signed', 'unsigned'))
    global INPUT_SIGNEDNESS
    previous = INPUT_SIGNEDNESS
    INPUT_SIGNEDNESS = val
    yield
    INPUT_SIGNEDNESS = previous

@contextlib.contextmanager
def silence_limits(min_, max_):
    '''Override SILENCE_MIN and SILENCE_MAX globals.'''
    global SILENCE_MIN, SILENCE_MAX
    old_min, old_max = SILENCE_MIN, SILENCE_MAX
    SILENCE_MIN, SILENCE_MAX = min_, max_
    yield
    SILENCE_MIN, SILENCE_MAX = old_min, old_max

class NoiseFilter(object):
    def __init__(self):
        pass

class BufferedClassFile(object):
    def __init__(self):
        self.s = StringIO.StringIO()

    def get_stream(self):
        return self.s

def chunked_samples(input_wave, chunk_seconds):
    '''
    Generator returning parsed and raw wave data one chunk at a time
    
    Yield a pair of (parsed wave samples, raw wave frames) from `input_wave` 
    in chunks of at most `chunk_seconds` of data. The actual number of frames 
    per chunk will vary with the input wave's frame rate. 
    '''
    frames_per_chunk = int(input_wave.getframerate() * chunk_seconds)
    while True:
        frames = input_wave.readframes(frames_per_chunk)
        yield list(parse_frames(frames, input_wave)), frames

def parse_frames(frames, input_wave):
    '''
    Generator to convert wave frames to sample data
    
    Requires `input_wave` reference to source wave file to get 
    wave metadata, e.g. sample width.
    '''
    # todo: coalesce samples for all channels into one?

    # pass frame data for all channels at once to frame_to_sample
    chunk_size = input_wave.getsampwidth() * input_wave.getnchannels()
    for i in xrange(0, len(frames), chunk_size):
        frame = frames[i:i + chunk_size]
        yield frame_to_sample(frame, input_wave)

def frame_to_sample(frame, wave_file):
    '''
    Convert one frame to one sample

    Note that we expect that the frame data will contain frames for all
    channels of the wave file. However, only the first channel will be 
    processed. The data from the remaining channels will be ignored!
    '''
    sample_storage_bytes = wave_file.getsampwidth()

    # handling only the first channel
    frame_data = frame[0:sample_storage_bytes]

    # Padding all samples to 4byte integer
    if sample_storage_bytes < 4:

        if INPUT_ENDIANNESS == 'little':
            frame_data_MSB = frame_data[sample_storage_bytes - 1]
        else:
            frame_data_MSB = frame_data[0]

        # Check if positive or negative and set the MSB accordigly
        if ord(frame_data_MSB) & 0x80:
            padding_MSB = '\xff'
            frame_data_MSB = chr(ord(frame_data_MSB) & ~0x80)
        else:
            padding_MSB = '\x00'

        # Set the middle padding
        padding = '\x00' * (4 - sample_storage_bytes - 1)

        if INPUT_ENDIANNESS == 'little':
            try:
                frame_data = frame_data + padding + padding_MSB
            except Exception,e:
                import pdb
                pdb.set_trace()
        else:
            frame_data = padding_MSB + padding + frame_data

    fmt = ''
    if INPUT_ENDIANNESS == 'little':
        fmt += '<'
    else:
        fmt += '>'

    if input_is_signed_data(wave_file):
        fmt += 'l'
    else:
        fmt += 'L'

    sample = struct.unpack(fmt, frame_data)
    return sample[0]

def input_is_signed_data(wave_file):
    '''
    Return True if the input contains signed date, False if unsigned

    Wave spec says:
    - sample width == 8 bit ==> unsigned, 
    - sample width > 8 bit ==> signed

    Implement this but permit override if INPUT_SIGNEDNESS global
    is explicitly set. 
    '''
    if INPUT_SIGNEDNESS is None:
        # no explicit override, follow spec
        return (wave_file.getsampwidth() > 1)
    else:
        return INPUT_SIGNEDNESS == 'signed'

def remove_silences(input_file, output_file):
    input_wave = wave.open(input_file)

    # Print audio setup
    logging.debug('Input wave params: {0}'.format(input_wave.getparams()))
    logging.debug('Frame rate: {0} Hz'.format(input_wave.getframerate()))

    buf = BufferedClassFile()

    # Open file we will write to.
    o = wave.open(buf.get_stream(), 'w')
    o.setparams((
        input_wave.getnchannels(),
        input_wave.getsampwidth(),
        input_wave.getframerate(),
        0,
        'NONE',
        'not compressed'
    ))

    high = False

    # lasthigh is used for post-rolling (save one second _after_ the one with noise)
    lasthigh = False

    # oldbuf is for pre-rolling (save one second _before_ the one with noise)
    oldbuf = ''

    try:
        for chunk_samples, chunk_frames in chunked_samples(input_wave, CHUNK_SECONDS):
            if len(chunk_samples) == 0:
                raise EOFError

            min_, max_ = min(chunk_samples), max(chunk_samples)

            # Print bounds
            logging.debug('min {0}, max {1}'.format(min_, max_))

            if max_ > SILENCE_MAX and min_ < SILENCE_MIN:
                high = True
            else:
                high = False

            # Write always if either is True.
            if lasthigh or high:

                if not lasthigh:
                    logging.debug('Pre-rolling...')
                    o.writeframes(oldbuf)

                if high:
                    logging.debug('...Recording...')
                else:
                    logging.debug('...Post-rolling')

                o.writeframes(chunk_frames)

            # prepare for post-roll
            lasthigh = high
            
            # prepare for pre-roll
            oldbuf = chunk_frames

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        input_wave.close()
        # TODO: encapsulate the following logic in the destuctor
        #       of BufferedClassFile
        buf.get_stream().flush()
        output_file.write(buf.get_stream().getvalue())
        o.close()
        logging.debug('Wrote {0} bytes.'.format(
            len(buf.get_stream().getvalue())
        ))

def main(*argv):
    parser = argparse.ArgumentParser(description='Remove silence from wave audio data.')
    parser.add_argument(
        '-i',
        '--input_filename', 
        default='-',
        help='Filename to read. Defaults to - for STDIN.'
    )
    parser.add_argument(
        'output_filename', 
        default='-',
        help='Filename to write to.'
    )
    parser.add_argument(
        '--silence-min',
        type=int,
        default=SILENCE_MIN,
        help='Minimum wave sample value to consider silence.'
    )
    parser.add_argument(
        '--silence-max',
        type=int,
        default=SILENCE_MAX,
        help='Maximum wave sample value to consider silence.'
    )
    parser.add_argument(
        '--input-big-endian',
        action='store_true',
        help='Ignore the Wave spec and interpret input as big endian. Not recommended.'
    )
    parser.add_argument(
        '--input-override-signedness',
        choices=("unsigned", "signed"),
        default=None,
        help='Ignore the Wave spec and interpres input with given signedness. Not recommended.'
    )
    args = parser.parse_args(argv[1:])

    input_filename = args.input_filename
    output_filename = args.output_filename

    input_file = sys.stdin if input_filename == '-' else open(input_filename, 'rb')

    with silence_limits(args.silence_min, args.silence_max):
        with input_endianness('big' if args.input_big_endian else 'little'):
            with input_signedness(args.input_override_signedness):
                with open(output_filename, 'wb') as output_file:
                    remove_silences(input_file, output_file)

    return 0

if __name__ == '__main__':
    sys.exit(main(*sys.argv))

