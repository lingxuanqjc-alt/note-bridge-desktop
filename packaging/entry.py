import multiprocessing

if __name__ == "__main__":
    # Dispatch frozen login children before importing the app or taking its single-instance lock.
    multiprocessing.freeze_support()
    from note_bridge.app import main

    raise SystemExit(main())
