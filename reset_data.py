from app import app, db, User, ChatMessage, JournalEntry, MessageAnalysis, ThreadSnapshot, Letter

with app.app_context():
    print("Before:")
    print(f"  Users:        {User.query.count()}")
    print(f"  Chats:        {ChatMessage.query.count()}")
    print(f"  Journal:      {JournalEntry.query.count()}")
    print(f"  Analyses:     {MessageAnalysis.query.count()}")
    print(f"  Threads:      {ThreadSnapshot.query.count()}")
    print(f"  Letters:      {Letter.query.count()}")

    MessageAnalysis.query.delete()
    ChatMessage.query.delete()
    JournalEntry.query.delete()
    ThreadSnapshot.query.delete()
    Letter.query.delete()
    User.query.delete()
    db.session.commit()

    print("\nAfter:")
    print(f"  Users:        {User.query.count()}")
    print(f"  Chats:        {ChatMessage.query.count()}")
    print(f"  Journal:      {JournalEntry.query.count()}")
    print(f"  Analyses:     {MessageAnalysis.query.count()}")
    print(f"  Threads:      {ThreadSnapshot.query.count()}")
    print(f"  Letters:      {Letter.query.count()}")

    print("\nData cleared. All tables remain in place.")
    print("The demo user was deleted too. Restart app.py to recreate it,")
    print("or register a new account in the browser.")