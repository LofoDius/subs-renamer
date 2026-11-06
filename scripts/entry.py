import sys

if __name__ == "__main__":
    if "--self-check" in sys.argv:
        from subs_renamer.diagnostics import main

        sys.exit(main())
    else:
        from subs_renamer.gui import main

        main()
