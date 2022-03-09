import os
import pickle
import platform
import sys
import time
from collections import deque

import chess
import cv2
import numpy as np
from pynput import keyboard

from board_basics import Board_basics
from broadcast import Broadcast
from helper import perspective_transform
from videocapture import Video_capture_thread

cap_index = 0
cap_api = cv2.CAP_ANY

# Lichess Token and Broadcast ID
token = os.environ.get("LICHESS_TOKEN")
broadcast_id = broadcast_id = "r9K4Vjgf"

# Load the games metadata from a single PGN file.
with open("initial_game.pgn") as f:
    pgn = f.read().split("\n\n\n")
pgn_games = pgn

for argument in sys.argv:
    if argument.startswith("cap="):
        cap_index = int("".join(c for c in argument if c.isdigit()))
        platform_name = platform.system()
        if platform_name == "Darwin":
            cap_api = cv2.CAP_AVFOUNDATION
        elif platform_name == "Linux":
            cap_api = cv2.CAP_V4L2
        else:
            cap_api = cv2.CAP_DSHOW
    elif argument.startswith("token="):
        token = argument[len("token=") :].strip()

MOTION_START_THRESHOLD = 1.0
HISTORY = 100
MAX_MOVE_MEAN = 50
COUNTER_MAX_VALUE = 3

move_fgbg = cv2.createBackgroundSubtractorKNN()
motion_fgbg = cv2.createBackgroundSubtractorKNN(history=HISTORY)

filename = "constants.bin"
infile = open(filename, "rb")
corners, side_view_compensation, rotation_count, roi_mask = pickle.load(infile)
infile.close()
board_basics = Board_basics(side_view_compensation, rotation_count)


broadcast = Broadcast(board_basics, token, broadcast_id, pgn_games, roi_mask)

video_capture_thread = Video_capture_thread()
video_capture_thread.daemon = True
video_capture_thread.capture = cv2.VideoCapture(cap_index, cap_api)
video_capture_thread.start()

# Keyboard Detection
def on_press(key):
    try:
        if key.char == ("u"):
            undo_moves()
    except AttributeError:
        pass


#
def undo_moves():
    input("\nEdit ongoing_games.pgn and press enter to continue.")

    with open("ongoing_games.pgn") as f:
        broadcast.internet_broadcast.pgn_games = f.read().split("\n\n\n")
    broadcast.internet_broadcast.push_current_pgn()
    print("Done updating broadcast.")

    # This should be turned in a method of Broadcast.
    # Should be updated to create a board for each pgn game.
    broadcast.board = chess.pgn.read_game(
        broadcast.internet_broadcast.pgn_games[0]
    ).board()


listener = keyboard.Listener(on_press=on_press)
listener.start()


pts1 = np.float32(
    [
        list(corners[0][0]),
        list(corners[8][0]),
        list(corners[0][8]),
        list(corners[8][8]),
    ]
)


def waitUntilMotionCompletes():
    counter = 0
    while counter < COUNTER_MAX_VALUE:
        frame = video_capture_thread.get_frame()
        frame = perspective_transform(frame, pts1)
        fgmask = motion_fgbg.apply(frame)
        ret, fgmask = cv2.threshold(fgmask, 250, 255, cv2.THRESH_BINARY)
        mean = fgmask.mean()
        if mean < MOTION_START_THRESHOLD:
            counter += 1
        else:
            counter = 0


def stabilize_background_subtractors():
    best_mean = float("inf")
    counter = 0
    while counter < COUNTER_MAX_VALUE:
        frame = video_capture_thread.get_frame()
        frame = perspective_transform(frame, pts1)
        move_fgbg.apply(frame)
        fgmask = motion_fgbg.apply(frame, learningRate=0.1)
        ret, fgmask = cv2.threshold(fgmask, 250, 255, cv2.THRESH_BINARY)
        mean = fgmask.mean()
        if mean >= best_mean:
            counter += 1
        else:
            best_mean = mean
            counter = 0

    best_mean = float("inf")
    counter = 0
    while counter < COUNTER_MAX_VALUE:
        frame = video_capture_thread.get_frame()
        frame = perspective_transform(frame, pts1)
        fgmask = move_fgbg.apply(frame, learningRate=0.1)
        ret, fgmask = cv2.threshold(fgmask, 250, 255, cv2.THRESH_BINARY)
        motion_fgbg.apply(frame)
        mean = fgmask.mean()
        if mean >= best_mean:
            counter += 1
        else:
            best_mean = mean
            counter = 0

    return frame


previous_frame = stabilize_background_subtractors()
board_basics.initialize_ssim(previous_frame)
broadcast.initialize_hog(previous_frame)
previous_frame_queue = deque(maxlen=10)
previous_frame_queue.append(previous_frame)
while not broadcast.board.is_game_over():

    # Allow to undo moves.
    # if keyboard.is_pressed("U"):
    #     input("Edit PGN and press Enter to continue:")

    sys.stdout.flush()
    frame = video_capture_thread.get_frame()
    frame = perspective_transform(frame, pts1)
    fgmask = motion_fgbg.apply(frame)
    ret, fgmask = cv2.threshold(fgmask, 250, 255, cv2.THRESH_BINARY)
    kernel = np.ones((11, 11), np.uint8)
    fgmask = cv2.erode(fgmask, kernel, iterations=1)
    mean = fgmask.mean()
    if mean > MOTION_START_THRESHOLD:
        # cv2.imwrite("motion.jpg", fgmask)
        waitUntilMotionCompletes()
        frame = video_capture_thread.get_frame()
        frame = perspective_transform(frame, pts1)
        fgmask = move_fgbg.apply(frame, learningRate=0.0)
        if fgmask.mean() >= 10.0:
            ret, fgmask = cv2.threshold(fgmask, 250, 255, cv2.THRESH_BINARY)
        # print("Move mean " + str(fgmask.mean()))
        if fgmask.mean() >= MAX_MOVE_MEAN:
            fgmask = np.zeros(fgmask.shape, dtype=np.uint8)
        motion_fgbg.apply(frame)
        move_fgbg.apply(frame, learningRate=1.0)
        last_frame = stabilize_background_subtractors()
        previous_frame = previous_frame_queue[0]

        if not broadcast.is_light_change(last_frame):
            for i in range(2):
                print(i)
                if not broadcast.register_move(
                    fgmask, previous_frame, last_frame
                ):
                    continue
                    # pass
                    # import uuid
                    # id = str(uuid.uuid1())
                    # cv2.imwrite(id+"frame_fail.jpg", last_frame)
                    # cv2.imwrite(id+"mask_fail.jpg", fgmask)
                    # cv2.imwrite(id+"background_fail.jpg", previous_frame)
                cv2.imwrite(
                    "images/" + broadcast.executed_moves[-1] + " frame.jpg",
                    last_frame,
                )
                cv2.imwrite(
                    "images/" + broadcast.executed_moves[-1] + " mask.jpg",
                    fgmask,
                )
                cv2.imwrite(
                    "images/"
                    + broadcast.executed_moves[-1]
                    + " background.jpg",
                    previous_frame,
                )

        previous_frame_queue = deque(maxlen=75)  # maxlen = 10
        previous_frame_queue.append(last_frame)
    else:
        move_fgbg.apply(frame)
        previous_frame_queue.append(frame)
cv2.destroyAllWindows()
time.sleep(2)
