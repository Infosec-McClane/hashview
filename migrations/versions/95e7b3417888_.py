"""empty message

Revision ID: 95e7b3417888
Revises: e2bc136a4e57
Create Date: 2020-12-31 16:49:48.116777

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '95e7b3417888'
down_revision = 'e2bc136a4e57'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('hashes', sa.Column('hash_type', sa.Integer(), nullable=False))
    op.drop_column('hashes', 'hashtype')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('hashes', sa.Column('hashtype', mysql.INTEGER(display_width=11), autoincrement=False, nullable=False))
    op.drop_column('hashes', 'hash_type')
    # ### end Alembic commands ###
