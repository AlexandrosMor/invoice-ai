"""Explicitly assign unowned pre-auth invoices to an existing user account."""
import argparse
from sqlalchemy import select, update
from app import Session, User, Record, normalized_email

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email",required=True,help="Existing account that should own the legacy local invoices")
    parser.add_argument("--confirm",action="store_true",help="Actually apply the ownership assignment")
    args=parser.parse_args()
    with Session() as db:
        user=db.scalar(select(User).where(User.email==normalized_email(args.email)))
        if not user: parser.error("No registered account matches that email; register first")
        unassigned=db.scalars(select(Record).where(Record.user_id.is_(None))).all()
        print(f"{len(unassigned)} legacy invoice(s) are unassigned.")
        if not args.confirm:
            print(f"Dry run only. Re-run with --confirm to assign them to {user.email}.")
            return
        db.execute(update(Record).where(Record.user_id.is_(None)).values(user_id=user.id))
        db.commit()
        print(f"Assigned {len(unassigned)} legacy invoice(s) to {user.email}.")

if __name__=="__main__": main()
